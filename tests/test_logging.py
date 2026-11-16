"""Exercise private logging, rotation diagnostics, and terminal status output."""

import asyncio
import logging
import os
import re
import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from threading import Event, Thread
from typing import NoReturn

import pytest
from pydantic import ValidationError
from rich.console import Console

from tunnel_switch.cli import serve
from tunnel_switch.config import Config, load_config
from tunnel_switch.logs import configure_logging
from tunnel_switch.status import run_status_display, status_panel
from tunnel_switch.tunnel import TunnelFailure, TunnelManager

PRIVATE_FILE_MODE = 0o600
DEFAULT_API_PORT = 8765


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, got {actual!r}")


def config(**changes: object) -> Config:
    """Build an isolated configuration with caller-supplied overrides."""
    return Config.model_validate(
        {
            "servers": [{"name": "one", "host": "one.example", "user": "tunnel"}],
            "proxy": {"ip": "127.0.0.1", "port": 1080},
            "start_on_launch": False,
            "state_file": None,
            "cooldown_seconds": 0,
            "http_proxy": {"enabled": False},
            **changes,
        },
    )


def test_logging_defaults_and_level_validation() -> None:
    """Validate default logging settings and reject unsupported options."""
    cfg = config()
    assert (
        cfg.logging.file
        == Path("~/.config/ssh-tunnel-switch/tunnel-switch.log").expanduser()
    ), "The default log path must reside in the user's configuration directory"
    _equal(cfg.logging.level, "INFO")
    _equal(config(logging={"level": "debug"}).logging.level, "DEBUG")
    assert config(logging={"file": None}).logging.file is None, (
        "File logging can be disabled"
    )
    with pytest.raises(ValidationError, match="level"):
        config(logging={"level": "LOUD"})
    with pytest.raises(ValidationError, match="Extra inputs"):
        config(logging={"stdout": True})


def test_log_file_resolves_beside_yaml_and_is_protected(tmp_path: Path) -> None:
    """Resolve relative log paths and refuse configuration or directory targets."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\nlogging:\n  file: logs/service.log\n",
    )
    assert (
        load_config(path).logging.file == (tmp_path / "logs" / "service.log").resolve()
    ), "Relative log paths must resolve beside the configuration"
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\nlogging:\n  file: config.yaml\n",
    )
    with pytest.raises(ValueError, match="must differ"):
        load_config(path)
    path.write_text("servers:\n  - host: one\nproxy: {}\nlogging:\n  file: .\n")
    with pytest.raises(ValueError, match="must be a regular file"):
        load_config(path)


def test_log_file_may_not_be_a_fifo(tmp_path: Path) -> None:
    """Reject FIFO log targets before a logging thread can block."""
    if sys.platform == "win32":
        pytest.skip("FIFOs are unavailable on Windows")
    path = tmp_path / "config.yaml"
    fifo = tmp_path / "tunnel.log"
    os.mkfifo(fifo)
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\nlogging:\n  file: tunnel.log\n",
    )
    # A FIFO would block the logging thread on its first write for ever.
    with pytest.raises(ValueError, match="must be a regular file"):
        load_config(path)


def test_state_file_may_not_equal_log_file(tmp_path: Path) -> None:
    """Prevent state persistence and logging from sharing a destination."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\n"
        "state_file: shared.log\nlogging:\n  file: shared.log\n",
    )
    with pytest.raises(ValueError, match="must differ"):
        load_config(path)


@pytest.fixture
def root_logging() -> Iterator[logging.Logger]:
    """Restore the root level and close handlers owned by the application."""
    root = logging.getLogger()
    before = root.level
    yield root
    for handler in [h for h in root.handlers if getattr(h, "_tunnel_switch", False)]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(before)


def test_configure_logging_writes_timestamped_private_file(
    tmp_path: Path,
    root_logging: logging.Logger,
) -> None:
    """Write access logs with UTC timestamps and owner-only permissions."""
    target = tmp_path / "private" / "service.log"
    settings = config(logging={"file": target}).logging
    _equal(configure_logging(settings, console=False), target)
    logging.getLogger("uvicorn.access").info(
        '127.0.0.1:1 - "GET /status HTTP/1.1" 200 OK',
    )
    for handler in root_logging.handlers:
        handler.flush()
    line = target.read_text().rstrip("\n")
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO uvicorn\.access: 127\.0\.0\.1:1 - "
        r'"GET /status HTTP/1\.1" 200 OK$',
        line,
    ), f"Unexpected access log format: {line!r}"
    if sys.platform != "win32":
        _equal(target.stat().st_mode & 0o777, PRIVATE_FILE_MODE)


def test_configure_logging_console_and_idempotence(
    tmp_path: Path,
    root_logging: logging.Logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Replace owned handlers on reconfiguration without duplicate emission."""
    target = tmp_path / "service.log"
    settings = config(logging={"file": None}).logging
    assert configure_logging(settings, console=True) is None, (
        "Console-only logging has no file"
    )
    logging.getLogger().warning("to stderr")
    assert "to stderr" in capsys.readouterr().err, "Console warnings must reach stderr"
    file_settings = config(logging={"file": target}).logging
    configure_logging(file_settings, console=False)
    configure_logging(file_settings, console=False)
    logging.getLogger().info("once")
    for handler in root_logging.handlers:
        handler.flush()
    _equal(target.read_text().count("once"), 1)
    # Reconfiguring replaced, not duplicated, our handlers.
    ours = [h for h in root_logging.handlers if getattr(h, "_tunnel_switch", False)]
    _equal(len(ours), 1)


def test_rollover_creates_rotated_private_files(
    tmp_path: Path,
    root_logging: logging.Logger,
) -> None:
    """Bound rotated backups and preserve private permissions after rollover."""
    target = tmp_path / "service.log"
    settings = config(logging={"file": target}).logging
    configure_logging(settings, console=False)
    logger = logging.getLogger()
    handler = next(
        h for h in root_logging.handlers if getattr(h, "_tunnel_switch", False)
    )
    # Shrink the threshold so emission alone triggers size-based rotation,
    # exercising the behaviour configure_logging actually configures.
    handler.maxBytes = 512
    for index in range(60):
        logger.info("line %s padded to grow the file %s", index, "x" * 100)
    handler.flush()
    rotated = tmp_path / "service.log.1"
    assert rotated.exists(), "Size-based rollover must create a backup"
    assert "line" in rotated.read_text(), "The backup must contain emitted records"
    # Rotation keeps at most the three configured backups.
    assert not (tmp_path / "service.log.4").exists(), (
        "Only three backups may be retained"
    )
    # The replacement file created by rotation stays private, and appends keep working.
    logger.info("after rollover")
    for owned in root_logging.handlers:
        if getattr(owned, "_tunnel_switch", False):
            owned.flush()
    assert "after rollover" in target.read_text(), (
        "Logging must continue after rollover"
    )
    if sys.platform != "win32":
        _equal(target.stat().st_mode & 0o777, PRIVATE_FILE_MODE)
        _equal(rotated.stat().st_mode & 0o777, PRIVATE_FILE_MODE)


def test_private_handler_refuses_symlinked_log_path(
    tmp_path: Path,
    root_logging: logging.Logger,
) -> None:
    """Leave symlink targets untouched when private logging refuses a path."""
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("requires O_NOFOLLOW")
    protected = tmp_path / "protected.txt"
    protected.write_text("secret material\n")
    target = tmp_path / "service.log"
    target.symlink_to(protected)
    settings = config(logging={"file": target}).logging
    configure_logging(settings, console=False)
    # The handler reports the refusal instead of following the link, so log
    # appends cannot be redirected onto a protected file.
    logging.getLogger().info("must not reach the protected file")
    for handler in root_logging.handlers:
        if getattr(handler, "_tunnel_switch", False):
            handler.flush()
    _equal(protected.read_text(), "secret material\n")
    assert target.is_symlink(), "Refusing a log path must not replace its symlink"


def test_private_handler_refuses_shared_directory(
    tmp_path: Path,
    root_logging: logging.Logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report unsafe directory permissions without creating a log file."""
    assert root_logging is logging.getLogger(), (
        "The fixture must manage the root logger"
    )
    if sys.platform == "win32":
        pytest.skip("POSIX directory modes")
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o777)
    settings = config(logging={"file": directory / "service.log"}).logging
    configure_logging(settings, console=False)
    logging.getLogger().info("must not be written to a shared directory")
    # No file appears where other users could swap the log path, and the
    # refusal is reported on stderr rather than raised into the service.
    assert not (directory / "service.log").exists(), (
        "Unsafe directories must remain unwritten"
    )
    assert (
        "log directory must not be group- or world-writable" in capsys.readouterr().err
    ), "Unsafe directory refusal must be reported on stderr"


def test_configure_logging_console_level_filters_by_severity(
    tmp_path: Path,
    root_logging: logging.Logger,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Filter routine console records while retaining warning diagnostics."""
    assert root_logging is logging.getLogger(), (
        "The fixture must manage the root logger"
    )
    assert tmp_path.is_dir(), "The temporary logging workspace must exist"
    settings = config(logging={"file": None}).logging
    configure_logging(settings, console=True, console_level=logging.WARNING)
    logger = logging.getLogger()
    logger.info("routine info hidden")
    logger.warning("serious warning shown")
    err = capsys.readouterr().err
    assert "routine info hidden" not in err, "Console INFO must be filtered"
    assert "serious warning shown" in err, "Console warnings must remain visible"


def status() -> dict[str, object]:
    """Provide a connected snapshot for terminal rendering tests."""
    return {
        "state": "connected",
        "connection_id": "9d7e9746-c4e2-4a43-8da5-674ab452cb91",
        "started_at": "2026-10-16T09:00:00+00:00",
        "server": "one",
        "proxy": {"ip": "127.0.0.1", "port": 1080},
        "last_error": None,
    }


def rendered(cfg: Config, current: dict[str, object]) -> str:
    """Render a status panel into a deterministic-width text buffer."""
    buffer = StringIO()
    Console(file=buffer, width=100).print(status_panel(cfg, current))
    return buffer.getvalue()


def test_status_panel_renders_state_server_and_error(tmp_path: Path) -> None:
    """Show connection identity, proxy endpoints, log path, and tunnel errors."""
    log_file = tmp_path / "svc.log"
    cfg = config(
        http_proxy={"enabled": True, "port": 8118},
        logging={"file": log_file},
    )
    text = rendered(cfg, status())
    assert "connected" in text, "The panel must show connected state"
    assert "one" in text, "The panel must name the server"
    assert "9d7e9746 (one)" in text, "The panel must pair connection ID and server"
    assert "127.0.0.1:1080" in text, "The panel must show the SOCKS endpoint"
    assert "127.0.0.1:8118" in text, "The panel must show the HTTP endpoint"
    assert log_file.name in text, "The panel must identify the log file"
    error = {
        **status(),
        "state": "disconnected",
        "last_error": "SSH exited with code 255",
    }
    assert "SSH exited with code 255" in rendered(cfg, error), (
        "The panel must show tunnel errors"
    )


def test_status_panel_shows_the_request_totals_per_connection() -> None:
    """Display current and last request totals without requiring counter data."""
    cfg = config(http_proxy={"enabled": True, "port": 8118})
    counted = {**status(), "requests": {"current": 12, "last": 3}}
    text = rendered(cfg, counted)
    assert "HTTP requests" in text, "Counted snapshots must label request totals"
    assert "12 current" in text, "The panel must show the current total"
    assert "3 last" in text, "The panel must show the last total"
    # A status dict without the counter keeps the rest of the panel intact.
    plain = rendered(cfg, status())
    assert "HTTP requests" not in plain, "Snapshots without counters must omit totals"
    assert "9d7e9746 (one)" in plain, (
        "Omitting totals must preserve connection identity"
    )


def test_status_display_renders_until_stop_event() -> None:
    """Render live manager status at least once and stop the display thread."""
    manager = TunnelManager(config())
    renders = []
    original = manager.status

    def spy() -> dict[str, object]:
        renders.append(original())
        return renders[-1]

    manager.status = spy
    stop = Event()
    thread = Thread(
        target=run_status_display,
        args=(manager, stop),
        kwargs={"console": Console(file=StringIO(), width=100)},
    )
    thread.start()
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), "The display thread must honor its stop event"
    # The panel rendered the live status at least once before stopping.
    assert renders, "The display must render at least one snapshot"
    _equal(renders[0]["state"], "disconnected")


def test_serve_shows_status_display_on_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root_logging: logging.Logger,
) -> None:
    """Run the TTY display beside uvicorn with actions and visible warnings."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - name: one\n    host: one.example\n    user: tunnel\n"
        "proxy:\n  ip: 127.0.0.1\n  port: 1080\n"
        "api:\n  token: abc\n"
        f"logging:\n  file: {tmp_path / 'service.log'}\n",
    )
    seen = {}
    runs = []
    monkeypatch.setattr("tunnel_switch.cli.is_tty", lambda: True)
    monkeypatch.setattr(
        "tunnel_switch.cli.uvicorn.run",
        lambda *_args, **kwargs: runs.append(kwargs),
    )
    monkeypatch.setattr(
        "tunnel_switch.cli.run_status_display",
        lambda manager, stop, **kwargs: seen.update(
            server=manager.status()["server"],
            log=manager.config.logging.file,
            state=manager.status()["state"],
            stop=stop,
            actions=sorted(kwargs["actions"]),
        ),
    )
    serve(path)
    _equal(seen["log"], tmp_path / "service.log")
    _equal(
        seen["actions"],
        [
            "connect",
            "disconnect",
            "reload",
            "restart",
            "rotate",
            "select",
        ],
    )
    # uvicorn keeps the main thread while the display runs alongside it.
    _equal(len(runs), 1)
    assert runs[0]["log_config"] is None, "Uvicorn must retain configured logging"
    # serve() stops the display once uvicorn has shut down.
    assert seen["stop"].is_set(), "Serving must stop the display after uvicorn exits"
    captured = capsys.readouterr()
    assert "Log file:" in captured.out, "Startup must identify the log destination"
    # The console handler keeps warnings visible behind the status panel, while
    # routine records are left to the log file.
    console = [
        h
        for h in root_logging.handlers
        if getattr(h, "_tunnel_switch", False)
        and isinstance(h, logging.StreamHandler)
        and not hasattr(h, "baseFilename")
    ]
    assert console, "TTY serving must retain a console handler"
    _equal(console[0].level, logging.WARNING)


def test_serve_without_tty_runs_uvicorn_and_skips_display(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_logging: logging.Logger,
) -> None:
    """Run uvicorn without starting a status display when stdout is not a TTY."""
    assert root_logging is logging.getLogger(), (
        "The fixture must manage the root logger"
    )
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - name: one\n    host: one.example\n    user: tunnel\n"
        "proxy:\n  ip: 127.0.0.1\n  port: 1080\napi:\n  token: abc\n"
        f"logging:\n  file: {tmp_path / 'service.log'}\n",
    )
    runs = []
    displays = []
    monkeypatch.setattr("tunnel_switch.cli.is_tty", lambda: False)
    monkeypatch.setattr(
        "tunnel_switch.cli.uvicorn.run",
        lambda *_args, **kwargs: runs.append(kwargs),
    )
    monkeypatch.setattr(
        "tunnel_switch.cli.run_status_display",
        lambda *args: displays.append(args),
    )
    serve(path)
    _equal(len(runs), 1)
    _equal(runs[0]["port"], DEFAULT_API_PORT)
    assert runs[0]["log_config"] is None, "Uvicorn must retain configured logging"
    assert not displays, "Non-TTY serving must not start the display"


def test_manager_logs_rotation_outcome(root_logging: logging.Logger) -> None:
    """Log a failed rotation at ERROR with its traceback and no success record."""
    records = []

    class Sink(logging.Handler):
        """Capture manager records for outcome inspection."""

        _tunnel_switch = True

        def emit(self, record: logging.LogRecord) -> None:
            """Retain each emitted record for the test."""
            records.append(record)

    root_logging.addHandler(Sink())
    manager = TunnelManager(config())
    with pytest.raises(TunnelFailure):
        asyncio_run_rotation(manager)
    failures = [r for r in records if "failed" in r.getMessage()]
    assert failures, "A failed rotation must emit a failure record"
    assert (
        failures[0].getMessage().startswith("Tunnel rotation to server one failed")
    ), "The failure must identify its server"
    # The failure is logged with its traceback for post-mortem diagnosis.
    assert failures[0].exc_info is not None, "Failures must include traceback details"
    _equal(failures[0].levelno, logging.ERROR)
    successes = [r for r in records if "connected" in r.getMessage()]
    assert not successes, "Failed rotations must not log success"


def test_manager_logs_every_server_tried_before_giving_up(
    root_logging: logging.Logger,
) -> None:
    """Name every failed server and summarize all attempts before giving up."""
    records = []

    class Sink(logging.Handler):
        """Capture per-server and aggregate rotation failures."""

        _tunnel_switch = True

        def emit(self, record: logging.LogRecord) -> None:
            """Retain each emitted record for the test."""
            records.append(record)

    root_logging.addHandler(Sink())
    settings = config(
        servers=[
            {"name": "one", "host": "one.example", "user": "tunnel"},
            {"name": "two", "host": "two.example", "user": "tunnel"},
        ],
    )
    manager = TunnelManager(settings)
    with pytest.raises(TunnelFailure, match="no route to host"):
        asyncio_run_rotation(manager)
    failures = [
        record.getMessage() for record in records if "failed" in record.getMessage()
    ]
    # The skipped server is named as it is abandoned, then every attempt is
    # summarised once so the log shows exactly what was tried and why.
    assert failures[0].startswith(
        "Tunnel rotation to server one failed; trying the next server",
    ), "The first failure must explain failover"
    assert failures[1].startswith("Tunnel rotation to server two failed"), (
        "The second failure must identify its server"
    )
    _equal(
        failures[2],
        (
            "Tunnel rotation failed on every server (2 tried): "
            "one: no route to host; two: no route to host"
        ),
    )
    assert not [message for message in failures if "connected to" in message], (
        "No failed attempt may report success"
    )


def asyncio_run_rotation(manager: TunnelManager) -> dict[str, object]:
    """Run a rotation with a deterministic socket failure and no subprocess."""

    async def fake_stop() -> None:
        manager.process = None

    def fail() -> NoReturn:
        message = "no route to host"
        raise OSError(message)

    manager._stop = fake_stop
    manager._ensure_socket_free = fail
    return asyncio.run(manager.rotate())
