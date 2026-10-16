import logging
import re
import sys
from pathlib import Path
import pytest
from pydantic import ValidationError
from tunnel_switch.config import Config, load_config
from tunnel_switch.logs import configure_logging
from tunnel_switch.status import status_panel
from tunnel_switch.tunnel import TunnelFailure, TunnelManager


def config(**changes):
    return Config.model_validate(
        {
            "servers": [{"name": "one", "host": "one.example", "user": "tunnel"}],
            "proxy": {"ip": "127.0.0.1", "port": 1080},
            "start_on_launch": False,
            "state_file": None,
            "cooldown_seconds": 0,
            "http_proxy": {"enabled": False},
            **changes,
        }
    )


def test_logging_defaults_and_level_validation():
    cfg = config()
    assert (
        cfg.logging.file
        == Path("~/.config/ssh-tunnel-switch/tunnel-switch.log").expanduser()
    )
    assert cfg.logging.level == "INFO"
    assert config(logging={"level": "debug"}).logging.level == "DEBUG"
    assert config(logging={"file": None}).logging.file is None
    with pytest.raises(ValidationError, match="level"):
        config(logging={"level": "LOUD"})
    with pytest.raises(ValidationError, match="Extra inputs"):
        config(logging={"stdout": True})


def test_log_file_resolves_beside_yaml_and_is_protected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\nlogging:\n  file: logs/service.log\n"
    )
    assert (
        load_config(path).logging.file == (tmp_path / "logs" / "service.log").resolve()
    )
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\nlogging:\n  file: config.yaml\n"
    )
    with pytest.raises(ValueError, match="must differ"):
        load_config(path)
    path.write_text("servers:\n  - host: one\nproxy: {}\nlogging:\n  file: .\n")
    with pytest.raises(ValueError, match="must be a file"):
        load_config(path)


def test_state_file_may_not_equal_log_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - host: one\nproxy: {}\n"
        "state_file: shared.log\nlogging:\n  file: shared.log\n"
    )
    with pytest.raises(ValueError, match="must differ"):
        load_config(path)


@pytest.fixture
def root_logging():
    root = logging.getLogger()
    before = root.level
    yield root
    for handler in [h for h in root.handlers if getattr(h, "_tunnel_switch", False)]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(before)


def test_configure_logging_writes_timestamped_private_file(tmp_path, root_logging):
    target = tmp_path / "private" / "service.log"
    settings = config(logging={"file": target}).logging
    assert configure_logging(settings, console=False) == target
    logging.getLogger("uvicorn.access").info(
        '127.0.0.1:1 - "GET /status HTTP/1.1" 200 OK'
    )
    for handler in root_logging.handlers:
        handler.flush()
    line = target.read_text().rstrip("\n")
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO uvicorn\.access: 127\.0\.0\.1:1 - "
        r'"GET /status HTTP/1\.1" 200 OK$',
        line,
    )
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600


def test_configure_logging_console_and_idempotence(tmp_path, root_logging, capsys):
    target = tmp_path / "service.log"
    settings = config(logging={"file": None}).logging
    assert configure_logging(settings, console=True) is None
    logging.getLogger().warning("to stderr")
    assert "to stderr" in capsys.readouterr().err
    file_settings = config(logging={"file": target}).logging
    configure_logging(file_settings, console=False)
    configure_logging(file_settings, console=False)
    logging.getLogger().info("once")
    for handler in root_logging.handlers:
        handler.flush()
    assert target.read_text().count("once") == 1
    # Reconfiguring replaced, not duplicated, our handlers.
    ours = [h for h in root_logging.handlers if getattr(h, "_tunnel_switch", False)]
    assert len(ours) == 1


def test_rollover_creates_rotated_private_files(tmp_path, root_logging):
    target = tmp_path / "service.log"
    settings = config(logging={"file": target}).logging
    configure_logging(settings, console=False)
    logger = logging.getLogger()
    handler = next(
        h for h in root_logging.handlers if getattr(h, "_tunnel_switch", False)
    )
    for index in range(20):
        logger.info("line %s padded to grow the file %s", index, "x" * 100)
    handler.flush()
    handler.doRollover()
    rotated = tmp_path / "service.log.1"
    assert rotated.exists()
    assert "line 19" in rotated.read_text()
    # The replacement file created by rotation stays private, and appends keep working.
    logger.info("after rollover")
    for owned in root_logging.handlers:
        if getattr(owned, "_tunnel_switch", False):
            owned.flush()
    assert "after rollover" in target.read_text()
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
        assert rotated.stat().st_mode & 0o777 == 0o600


def status():
    return {
        "state": "connected",
        "connection_id": "9d7e9746-c4e2-4a43-8da5-674ab452cb91",
        "started_at": "2026-10-16T09:00:00+00:00",
        "server": "one",
        "proxy": {"ip": "127.0.0.1", "port": 1080},
        "last_error": None,
    }


def rendered(cfg, current, log_file=None):
    from io import StringIO
    from rich.console import Console

    buffer = StringIO()
    Console(file=buffer, width=100).print(status_panel(cfg, current, log_file))
    return buffer.getvalue()


def test_status_panel_renders_state_server_and_error(tmp_path):
    cfg = config(http_proxy={"enabled": True, "port": 8118})
    log_file = Path("/tmp/svc.log")
    text = rendered(cfg, status(), log_file)
    assert "connected" in text
    assert "one" in text
    assert "9d7e9746" in text
    assert "127.0.0.1:1080" in text
    assert "127.0.0.1:8118" in text
    assert log_file.name in text
    error = {
        **status(),
        "state": "disconnected",
        "last_error": "SSH exited with code 255",
    }
    assert "SSH exited with code 255" in rendered(cfg, error)


def test_status_display_renders_until_stop_event(tmp_path):
    from io import StringIO
    from threading import Event, Thread

    from rich.console import Console

    from tunnel_switch.status import run_status_display

    manager = TunnelManager(config())
    renders = []
    original = manager.status

    def spy():
        renders.append(original())
        return renders[-1]

    manager.status = spy
    stop = Event()
    thread = Thread(
        target=run_status_display,
        args=(
            config(),
            manager,
            tmp_path / "svc.log",
            stop,
            Console(file=StringIO(), width=100),
        ),
    )
    thread.start()
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    # The panel rendered the live status at least once before stopping.
    assert renders and renders[0]["state"] == "disconnected"


def test_serve_shows_status_display_on_tty(tmp_path, monkeypatch, capsys):
    from tunnel_switch.cli import serve

    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - name: one\n    host: one.example\n    user: tunnel\n"
        "proxy:\n  ip: 127.0.0.1\n  port: 1080\n"
        "api:\n  token: abc\n"
        f"logging:\n  file: {tmp_path / 'service.log'}\n"
    )
    seen = {}
    runs = []
    monkeypatch.setattr("tunnel_switch.cli.is_tty", lambda: True)
    monkeypatch.setattr(
        "tunnel_switch.cli.uvicorn.run", lambda *args, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        "tunnel_switch.cli.run_status_display",
        lambda cfg, manager, log_file, stop: seen.update(
            server=manager.status()["server"],
            log=log_file,
            state=manager.status()["state"],
            stop=stop,
        ),
    )
    serve(path)
    assert seen["log"] == tmp_path / "service.log"
    # uvicorn keeps the main thread while the display runs alongside it.
    assert len(runs) == 1
    assert runs[0]["log_config"] is None
    # serve() stops the display once uvicorn has shut down.
    assert seen["stop"].is_set()
    assert "Log file:" in capsys.readouterr().out


def test_serve_without_tty_runs_uvicorn_and_skips_display(tmp_path, monkeypatch):
    from tunnel_switch.cli import serve

    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - name: one\n    host: one.example\n    user: tunnel\n"
        "proxy:\n  ip: 127.0.0.1\n  port: 1080\napi:\n  token: abc\n"
        f"logging:\n  file: {tmp_path / 'service.log'}\n"
    )
    runs = []
    displays = []
    monkeypatch.setattr("tunnel_switch.cli.is_tty", lambda: False)
    monkeypatch.setattr(
        "tunnel_switch.cli.uvicorn.run", lambda *args, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        "tunnel_switch.cli.run_status_display",
        lambda *args: displays.append(args),
    )
    serve(path)
    assert len(runs) == 1
    assert runs[0]["port"] == 8765
    assert runs[0]["log_config"] is None
    assert not displays


def test_manager_logs_rotation_outcome(tmp_path, root_logging):
    records = []

    class Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    root_logging.addHandler(Sink())
    manager = TunnelManager(config())
    with pytest.raises(TunnelFailure):
        asyncio_run_rotation(manager)
    failures = [m for m in records if "failed" in m]
    assert failures
    assert failures[0].startswith("Tunnel rotation to server one failed:")
    successes = [m for m in records if "connected" in m]
    assert not successes


def asyncio_run_rotation(manager):
    import asyncio

    async def fake_stop():
        manager.process = None

    def fail():
        raise OSError("no route to host")

    manager._stop = fake_stop
    manager._ensure_socket_free = fail
    return asyncio.run(manager.rotate())
