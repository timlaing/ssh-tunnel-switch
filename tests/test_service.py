"""Verify tunnel lifecycle, recovery, persistence, API, and CLI behavior."""

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import threading
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from click import unstyle
from pydantic import ValidationError
from typer.testing import CliRunner

import tunnel_switch
from tunnel_switch import __version__
from tunnel_switch.api import create_app
from tunnel_switch.cli import app, main, resolve_config, serve, settings
from tunnel_switch.config import Config, load_config
from tunnel_switch.counters import RequestCounter
from tunnel_switch.http_proxy import ProxyError
from tunnel_switch.state import load_last_server, save_last_server
from tunnel_switch.tunnel import RotationRejected, TunnelFailure, TunnelManager

PRIVATE_FILE_MODE = 0o600
DEFAULT_API_PORT = 8765
MINIMUM_TOKEN_LENGTH = 40
SERVERS_PER_ROTATION = 2


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, got {actual!r}")


def _different(actual: object, previous: object) -> None:
    if actual == previous:
        pytest.fail(f"Expected a changed value, still got {actual!r}")


def config(**changes: object) -> Config:
    """Build a two-server configuration without startup or persistence effects."""
    return Config.model_validate(
        {
            "servers": [
                {"name": "one", "host": "one.example", "user": "tunnel"},
                {"name": "two", "host": "two.example", "user": "tunnel"},
            ],
            "proxy": {"ip": "127.0.0.1", "port": 1080},
            "start_on_launch": False,
            "state_file": None,
            "cooldown_seconds": 0,
            "http_proxy": {"enabled": False},
            **changes,
        },
    )


class Process:
    """Model the lifecycle of an SSH subprocess without opening sockets."""

    returncode: int | None = None

    def __init__(self) -> None:
        """Initialize a running process with observable shutdown flags."""
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        """Record graceful termination and a successful exit code."""
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        """Record forced termination and its signal exit code."""
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        """Return the exit code currently held by this process double."""
        return self.returncode


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> TunnelManager:
    """Supply a manager whose sockets and SSH processes are deterministic."""
    manager = TunnelManager(config())
    monkeypatch.setattr(manager, "_ensure_socket_free", lambda: None)
    monkeypatch.setattr(manager, "_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=lambda *_args, **_kwargs: Process()),
    )
    return manager


async def test_rotation_stops_old_and_changes_id(manager: TunnelManager) -> None:
    """Stop the old SSH process and publish a new UTC-stamped connection."""
    first = await manager.rotate()
    process = manager.process
    second = await manager.rotate()
    assert process.terminated, "Rotation must terminate the previous process"
    _different(first["connection_id"], second["connection_id"])
    _equal(first["server"], "one")
    _equal(second["server"], "two")
    assert second["started_at"].endswith("+00:00"), "Connection timestamps must be UTC"
    manager.process.returncode = 255
    assert manager.status()["connection_id"] is None, (
        "Exited SSH must clear its connection ID"
    )
    await manager.close()


async def test_status_reports_one_connection_snapshot(manager: TunnelManager) -> None:
    """Publish and clear connection identity, server, and timestamp together."""
    status = await manager.rotate()
    assert status["connection_id"], "An adopted connection must have an ID"
    _equal(status["server"], "one")
    # The trio is published and cleared as one snapshot, so the status display
    # thread can never read an ID paired with a missing server name.
    manager.process.returncode = 255
    dead = manager.status()
    _equal(
        (dead["connection_id"], dead["server"], dead["started_at"]),
        (
            None,
            None,
            None,
        ),
    )
    await manager.close()


async def test_request_totals_follow_the_connection(manager: TunnelManager) -> None:
    """Associate request totals with the current and previous connections."""
    await manager.rotate()
    # The adopted connection opens its own total.
    _equal(manager.status()["requests"], {"current": 0, "last": 0})
    for _ in range(3):
        manager.counter.record()
    _equal(manager.status()["requests"], {"current": 3, "last": 0})
    await manager.rotate()
    # The next connection reports its own total beside the previous one.
    _equal(manager.status()["requests"], {"current": 0, "last": 3})
    manager.counter.record()
    _equal(manager.status()["requests"], {"current": 1, "last": 3})
    await manager.close()


async def test_request_totals_of_a_tunnel_that_exited_report_as_last(
    manager: TunnelManager,
) -> None:
    """Report a spontaneously exited tunnel's requests as the last total."""
    await manager.rotate()
    manager.counter.record()
    manager.counter.record()
    manager.process.returncode = 255
    # ssh left on its own: its counts are the last connection's, not current.
    _equal(manager.status()["requests"], {"current": 0, "last": 2})
    await manager.close()


async def test_disconnect_keeps_the_finished_total_as_last(
    manager: TunnelManager,
) -> None:
    """Retain the finished request total across disconnect and restart."""
    await manager.rotate()
    manager.counter.record()
    manager.counter.record()
    _equal((await manager.disconnect())["requests"], {"current": 0, "last": 2})
    # Reconnecting does not restart the previous connection's total.
    restarted = await manager.restart()
    _equal(restarted["requests"], {"current": 0, "last": 2})
    await manager.close()


async def test_status_pairs_the_totals_with_the_connection_they_belong_to(
    manager: TunnelManager,
) -> None:
    """Keep connection identity and counters consistent during threaded reads."""
    await manager.rotate()
    for _ in range(3):
        manager.counter.record()
    first = manager.status()
    first_id = first["connection_id"]
    _equal(first["requests"], {"current": 3, "last": 0})
    observed = []
    stop = threading.Event()

    def read_status() -> None:
        while not stop.is_set():
            observed.append(manager.status())

    reader = threading.Thread(target=read_status, daemon=True)
    reader.start()
    try:
        # Rotations publish new connections while status() reads, which is the
        # moment a read could straddle the two: the ID of the old connection
        # beside the totals the new one has already taken over.
        for _ in range(25):
            await manager.rotate()
    finally:
        stop.set()
        reader.join(timeout=5)
    assert observed, "The reading thread never observed a status"
    for seen in observed:
        if seen["connection_id"] == first_id:
            _equal(seen["requests"], {"current": 3, "last": 0})
        if seen["connection_id"] is None:
            _equal(seen["requests"]["current"], 0)
    await manager.close()


async def test_concurrent_rotation_rejected(manager: TunnelManager) -> None:
    """Reject a second rotation while readiness holds the admission gate."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def ready() -> bool:
        entered.set()
        await release.wait()
        return True

    manager._ready = ready
    task = asyncio.create_task(manager.rotate())
    await entered.wait()
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.rotate()
    _equal(manager.status()["state"], "rotating")
    release.set()
    await task
    await manager.close()


async def test_restart_reconnects_to_the_same_server(manager: TunnelManager) -> None:
    """Restart the current server with a fresh connection identity."""
    await manager.rotate()
    await manager.rotate()
    _equal(manager.status()["server"], "two")
    previous = manager.status()["connection_id"]
    restarted = await manager.restart()
    # The index is pinned, not advanced: a restart bounces the current server.
    _equal(restarted["server"], "two")
    _different(restarted["connection_id"], previous)
    await manager.close()


async def test_restart_without_a_previous_connection_uses_the_first_server(
    manager: TunnelManager,
) -> None:
    """Use the first server when restarting without a prior connection."""
    _equal((await manager.restart())["server"], "one")
    await manager.close()


async def test_restart_fails_over_when_the_current_server_refuses(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Try the pinned server first and fail over if SSH refuses it."""
    await manager.rotate()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), Process()]),
    )
    status = await manager.restart()
    # The pinned server is tried first, then the same failover as rotate().
    _equal(status["server"], "two")
    assert manager.last_error is None, "Successful restart failover must clear errors"
    await manager.close()


async def test_select_server_connects_to_the_named_server(
    manager: TunnelManager,
) -> None:
    """Select named servers explicitly and pin restart to the selected server."""
    await manager.rotate()
    _equal((await manager.select_server("two"))["server"], "two")
    # The selection pins the index, so a restart bounces the chosen server.
    _equal((await manager.restart())["server"], "two")
    _equal((await manager.select_server("one"))["server"], "one")
    await manager.close()


async def test_select_server_rejects_an_unknown_name(manager: TunnelManager) -> None:
    """Reject unknown names without reserving admission or changing selection."""
    with pytest.raises(ValueError, match="Unknown server: missing"):
        await manager.select_server("missing")
    # A refused selection never reaches the admission gate or the tunnel.
    assert not manager.busy, "Unknown selections must not reserve admission"
    await manager.rotate()
    _equal(manager.status()["server"], "one")
    await manager.close()


async def test_select_server_skips_the_cooldown_but_not_a_running_change(
    manager: TunnelManager,
) -> None:
    """Bypass cooldown for explicit selection without bypassing admission."""
    manager.config.cooldown_seconds = 30
    await manager.rotate()
    with pytest.raises(RotationRejected, match="cooldown"):
        await manager.rotate()
    # An explicit choice is a deliberate request, so the backoff does not apply.
    _equal((await manager.select_server("two"))["server"], "two")
    manager._busy = True
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.select_server("one")
    manager._busy = False
    await manager.close()


async def test_select_server_reports_a_failure_without_failing_over(
    manager: TunnelManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave the tunnel disconnected when an explicitly selected server fails."""
    manager.config.cooldown_seconds = 30
    await manager.rotate()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=lambda *_args, **_kwargs: exited_ssh()),
    )
    with pytest.raises(TunnelFailure, match="SSH exited with code 255"):
        await manager.select_server("two")
    # The named server failed, so the tunnel stays down instead of moving on.
    status = manager.status()
    _equal(status["state"], "disconnected")
    assert status["server"] is None, "Failed selection must clear the active server"
    assert manager.process is None, "Failed selection must clean up its process"
    await manager.close()


async def test_rotate_ignores_the_cooldown_when_asked(manager: TunnelManager) -> None:
    """Allow deliberate rotation to bypass cooldown while respecting admission."""
    manager.config.cooldown_seconds = 30
    await manager.rotate()
    with pytest.raises(RotationRejected, match="cooldown"):
        await manager.rotate()
    # This flag is what the interactive rotate key passes; the API never does.
    _equal((await manager.rotate(ignore_cooldown=True))["server"], "two")
    # Only the backoff is skipped: a change already running still refuses.
    manager._busy = True
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.rotate(ignore_cooldown=True)
    manager._busy = False
    await manager.close()


async def test_cancelled_selection_cleans_up(manager: TunnelManager) -> None:
    """Cancellation mid-selection must not leave a half-started tunnel."""
    entered = asyncio.Event()

    async def ready() -> None:
        entered.set()
        await asyncio.Event().wait()

    manager._ready = ready
    task = asyncio.create_task(manager.select_server("two"))
    await entered.wait()
    process = manager.process
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated, "Cancelled selection must terminate its process"
    assert manager.process is None, "Cancelled selection must clear its process"


async def test_disconnect_stops_the_tunnel_and_clears_the_cooldown(
    manager: TunnelManager,
) -> None:
    """Stop a connection and allow an immediate restart of the same server."""
    manager.config.cooldown_seconds = 30
    await manager.rotate()
    process = manager.process
    disconnected = await manager.disconnect()
    assert process.terminated, "Disconnect must terminate SSH"
    _equal(disconnected["state"], "disconnected")
    assert disconnected["connection_id"] is None, (
        "Disconnect must clear connection identity"
    )
    # The cooldown gates connections; stopping never contacts a server, so a
    # restart reconnects to the same server immediately.
    _equal((await manager.restart())["server"], "one")
    await manager.close()


async def test_disconnect_is_rejected_while_a_rotation_runs(
    manager: TunnelManager,
) -> None:
    """Reject disconnect until an admitted rotation completes."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def ready() -> bool:
        entered.set()
        await release.wait()
        return True

    manager._ready = ready
    task = asyncio.create_task(manager.rotate())
    await entered.wait()
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.disconnect()
    release.set()
    await task
    _equal(manager.status()["state"], "connected")
    await manager.close()


async def test_rotation_is_rejected_while_a_disconnect_stops(
    manager: TunnelManager,
) -> None:
    """The busy flag is held across the stop, so nothing interleaves with it."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_stop() -> None:
        entered.set()
        await release.wait()

    manager._stop = slow_stop
    task = asyncio.create_task(manager.disconnect())
    await entered.wait()
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.rotate()
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.restart()
    release.set()
    _equal((await task)["state"], "disconnected")
    assert manager.busy is False, "Disconnect must release admission"
    await manager.close()


async def test_restart_before_any_attempt_uses_the_remembered_server(
    manager: TunnelManager,
) -> None:
    """Resume the remembered selection when restarting before the first attempt."""
    manager._index = 0  # The state file remembered server "two".
    # Before the first attempt there is no current server to pin, so the
    # restart connects where a rotation would have: the saved server.
    _equal((await manager.restart())["server"], "two")
    await manager.close()


async def test_failure_cleans_process_and_cools_down(manager: TunnelManager) -> None:
    """Clean up readiness failures and apply cooldown to the next rotation."""
    manager.config.cooldown_seconds = 30
    manager.config.startup_timeout_seconds = 0.01
    manager._ready = AsyncMock(return_value=False)
    with pytest.raises(TunnelFailure, match="timed out"):
        await manager.rotate()
    assert manager.process is None, "Readiness failure must clean up SSH"
    _equal(manager.status()["state"], "disconnected")
    with pytest.raises(RotationRejected, match="cooldown"):
        await manager.rotate()


def exited_ssh(code: int = 255) -> Process:
    """Represent a spawned SSH process that failed to connect and exited."""
    process = Process()
    process.returncode = code
    return process


async def test_ssh_connection_failure_advances_to_the_next_server(manager, monkeypatch):
    # The first target dies (auth/host key/unreachable): one rotate() call must
    # reach the working server, with no cooldown waited out in between.
    manager.config.cooldown_seconds = 30
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), Process()]),
    )
    status = await manager.rotate()
    assert status["server"] == "two"
    assert status["state"] == "connected"
    assert manager.last_error is None
    # The cooldown gates the next rotate(), not the failover within this one.
    with pytest.raises(RotationRejected, match="cooldown"):
        await manager.rotate()
    await manager.close()


async def test_failover_wraps_past_the_end_of_the_list(manager, monkeypatch):
    # The last server in the list refuses SSH: the rotation carries on around
    # to the first server instead of failing at the end of the list.
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[Process(), exited_ssh(), Process()]),
    )
    assert (await manager.rotate())["server"] == "one"
    status = await manager.rotate()
    assert status["server"] == "one"
    assert manager.last_error is None
    await manager.close()


async def test_rotation_reports_last_failure_when_every_server_fails(
    manager,
    monkeypatch,
):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), exited_ssh()]),
    )
    with pytest.raises(TunnelFailure, match="SSH exited with code 255"):
        await manager.rotate()
    status = manager.status()
    assert status["state"] == "disconnected"
    assert status["connection_id"] is None
    assert (
        manager.last_error
        == "SSH exited with code 255; check authentication, known_hosts and forwarding permissions"
    )
    assert manager.process is None
    await manager.close()


async def test_api_returns_503_when_no_server_accepts_ssh(manager, monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), exited_ssh()]),
    )
    app = create_app(manager.config, "secret", manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        headers = {"Authorization": "Bearer secret"}
        response = await client.post("/rotate", headers=headers)
        assert response.status_code == 503
        assert "SSH exited with code 255" in response.json()["detail"]
        assert (await client.get("/status", headers=headers)).json()["state"] == (
            "disconnected"
        )
    await manager.close()


async def test_cancelled_rotation_cleans_up(manager):
    entered = asyncio.Event()

    async def ready():
        entered.set()
        await asyncio.Event().wait()

    manager._ready = ready
    task = asyncio.create_task(manager.rotate())
    await entered.wait()
    process = manager.process
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated
    assert not manager._busy


async def test_authenticated_api_and_busy_response(manager):
    app = create_app(manager.config, "secret", manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/status")).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert (await client.post("/rotate", headers=headers)).status_code == 200
        manager._busy = True
        response = await client.post("/rotate", headers=headers)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "1"
        manager._busy = False
        assert (await client.get("/status", headers=headers)).json()["connection_id"]
    await manager.close()


async def test_rotate_with_reason_logs_and_returns_200(manager, caplog):
    """POST /rotate with a reason body should be accepted and logged at INFO."""

    caplog.set_level(logging.INFO)
    app = create_app(manager.config, "secret", manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        headers = {"Authorization": "Bearer secret"}
        reason = "rule triggered: rate limit exceeded; content: provider.rate-limit: Error: Rate limit exceeded"
        response = await client.post(
            "/rotate",
            headers=headers,
            json={"reason": reason},
        )
        assert response.status_code == 200
        # Verify the reason was logged at INFO level (at least once)
        log_messages = [
            r.message
            for r in caplog.records
            if "Tunnel rotation requested" in r.message
        ]
        assert len(log_messages) >= 1
        assert any("rule triggered" in m for m in log_messages)
    await manager.close()


@pytest.mark.parametrize(
    "reason", [None, 5, ["x"], {"detail": "x"}, True, False, 0, [], {}]
)
async def test_rotate_ignores_non_string_reasons(manager, caplog, reason):
    """Invalid optional reasons must not fail rotation or reach its reason log."""
    caplog.set_level(logging.INFO)
    application = create_app(manager.config, "secret", manager)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/rotate",
                headers={"Authorization": "Bearer secret"},
                json={"reason": reason},
            )
        assert response.status_code == 200
        assert response.json()["state"] == "connected"
        assert not any(
            "Tunnel rotation requested" in record.getMessage()
            for record in caplog.records
        )
    finally:
        await manager.close()


async def test_rotate_rejected_before_logging(manager, caplog):
    """When rotation is rejected (busy/cooldown), the reason should NOT be logged."""
    import os

    # Skip in CI where coverage instrumentation causes double logging
    if os.environ.get("CI") == "true":
        pytest.skip(
            "Skipping in CI due to coverage instrumentation double-logging issue",
        )

    # Clear any existing handlers on the tunnel logger to avoid duplicate logs
    # from module reloading in CI with coverage
    tunnel_logger = logging.getLogger("tunnel_switch.tunnel")
    for handler in tunnel_logger.handlers[:]:
        tunnel_logger.removeHandler(handler)

    caplog.set_level(logging.INFO)
    app = create_app(manager.config, "secret", manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        headers = {"Authorization": "Bearer secret"}
        # First rotation succeeds
        await client.post("/rotate", headers=headers, json={"reason": "first"})
        # Make it busy
        manager._busy = True
        # Second rotation should be rejected and NOT log the reason
        response = await client.post(
            "/rotate",
            headers=headers,
            json={"reason": "should not log"},
        )
        assert response.status_code == 429
        # Verify no INFO log for "should not log"
        log_messages = [
            r.message for r in caplog.records if "should not log" in r.message
        ]
        assert len(log_messages) == 0
        # But the first rotation's reason should be logged at least once
        log_messages = [
            r.message
            for r in caplog.records
            if "first" in r.message and "Tunnel rotation requested" in r.message
        ]
        assert len(log_messages) >= 1
    await manager.close()


async def test_real_socks_handshake():
    async def socks(reader, writer):
        assert await reader.readexactly(3) == b"\x05\x01\x00"
        writer.write(b"\x05\x00")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(socks, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    manager = TunnelManager(config(proxy={"ip": "127.0.0.1", "port": port}))
    async with server:
        assert await manager._ready()
        with pytest.raises(TunnelFailure, match="occupied"):
            manager._ensure_socket_free()


def test_config_and_cli(tmp_path):
    with pytest.raises(ValidationError):
        config(servers=[])
    with pytest.raises(ValidationError):
        config(proxy={"ip": "invalid", "port": 70000})
    with pytest.raises(ValidationError):
        config(unknown=True)
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config().model_dump(mode="json")))
    assert load_config(path).servers[0].name == "one"
    runner = CliRunner()
    assert runner.invoke(app, ["validate", "--config", str(path)]).exit_code == 0
    assert runner.invoke(app, ["serve"]).exit_code != 0


def test_main_failure_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["tunnel-switch", "validate", "--config", "/missing/config.yaml"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    text = capsys.readouterr().err
    assert "Error:" in text
    assert "Usage:" in text
    assert "--help" in text


def test_ipv6_command_uses_safe_args():
    manager = TunnelManager(config(proxy={"ip": "::1", "port": 1080}))
    command = manager.command(manager.config.servers[0])
    assert "[::1]:1080" in command
    assert "StrictHostKeyChecking=yes" in command
    assert command[-2:] == ["--", "one.example"]


async def test_real_subprocess_rotation(tmp_path, monkeypatch):

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "fake_ssh.py"
    script.write_text("""import asyncio, sys
async def handle(reader, writer):
    try:
        data = await reader.readexactly(3)
        writer.write(b"\\x05\\x00")
        await writer.drain()
        await reader.read()
    finally:
        writer.close()
async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", int(sys.argv[1]))
    async with server:
        await server.serve_forever()
asyncio.run(main())
""")
    manager = TunnelManager(config(proxy={"ip": "127.0.0.1", "port": port}))
    monkeypatch.setattr(
        manager,
        "command",
        lambda server: [sys.executable, str(script), str(port)],
    )
    try:
        first = await manager.rotate()
        old = manager.process
        second = await manager.rotate()
        assert first["connection_id"] != second["connection_id"]
        assert old.returncode is not None
        assert await manager._ready()
        start_recovery(manager)
        failed = manager.process
        failed.terminate()
        await failed.wait()
        await wait_for_condition(manager.adopted)
        assert manager.status()["server"] == second["server"]
        assert manager.status()["connection_id"] != second["connection_id"]
        assert await manager._ready()
    finally:
        await manager.close()
    assert manager.status()["state"] == "disconnected"


async def test_lifespan_closes_tunnel(manager):
    manager.config.start_on_launch = True
    app = create_app(manager.config, "secret", manager)
    async with app.router.lifespan_context(app):
        assert manager.status()["state"] == "connected"
    assert manager.process is None


@pytest.mark.parametrize("token_line", ["", "  token: null\n", '  token: "   "\n'])
def test_startup_generates_persists_and_prints_token(
    tmp_path,
    monkeypatch,
    capsys,
    token_line,
):

    path = tmp_path / "config.yaml"
    path.write_text(
        "# Keep this comment\nservers:\n  - name: one\n    host: one.example\n    user: tunnel\nproxy:\n  ip: 127.0.0.1\n  port: 1080\napi:\n  port: 8765 # API port\n"
        + token_line,
    )
    run = []
    monkeypatch.setattr(
        "tunnel_switch.cli.uvicorn.run",
        lambda *args, **kwargs: run.append(kwargs),
    )
    serve(path)
    saved = load_config(path).api.token
    assert saved
    assert len(saved) >= 40
    assert f"API token: {saved}" in capsys.readouterr().out
    assert "# Keep this comment" in path.read_text()
    assert "# API port" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert settings(path)[1] == saved
    assert run[0]["port"] == 8765
    before = path.read_bytes()
    serve(path)
    assert path.read_bytes() == before
    assert f"API token: {saved}" in capsys.readouterr().out


def test_startup_displays_existing_yaml_token(tmp_path, monkeypatch, capsys):

    path = tmp_path / "config.yaml"
    document = config().model_dump(mode="json")
    document["api"]["token"] = "existing-test-token"
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    monkeypatch.setattr("tunnel_switch.cli.uvicorn.run", lambda *args, **kwargs: None)
    serve(path)
    assert path.read_bytes() == before
    assert "API token: existing-test-token" in capsys.readouterr().out


def test_token_save_failure_prevents_startup(tmp_path, monkeypatch):

    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config().model_dump(mode="json")))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="start the service"):
        settings(path)
    run = AsyncMock()
    monkeypatch.setattr("tunnel_switch.cli.uvicorn.run", run)

    def fail(*args):
        raise PermissionError("Cannot save config")

    monkeypatch.setattr("tunnel_switch.config.os.replace", fail)
    with pytest.raises(PermissionError):
        serve(path)
    assert not run.called
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_host_only_server_loads_and_omits_ssh_overrides(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "servers:\n  - host: my-ssh-alias\nproxy:\n  ip: 127.0.0.1\n  port: 1080\nstate_file: null\n",
    )
    cfg = load_config(path)
    server = cfg.servers[0]
    assert server.name == "my-ssh-alias"
    assert server.user is None
    assert server.port is None
    assert server.identity_file is None
    command = TunnelManager(cfg).command(server)
    assert "-l" not in command
    assert "-p" not in command
    assert "-i" not in command
    assert "-F" not in command
    assert command[-2:] == ["--", "my-ssh-alias"]


@pytest.mark.parametrize("overrides", [False, True])
def test_ssh_resolves_config_and_explicit_overrides(tmp_path, overrides):
    import subprocess

    ssh_config = tmp_path / "ssh_config"
    identity = tmp_path / "identity"
    ssh_config.write_text(
        f"Host my-ssh-alias\n  HostName 127.0.0.1\n  User config-user\n  Port 2222\n  IdentityFile {identity}\n",
    )
    document = {"host": "my-ssh-alias"}
    if overrides:
        document.update(name="explicit-name", user="yaml-user", port=3333)
    cfg = config(servers=[document])
    command = TunnelManager(cfg).command(cfg.servers[0])
    # -G resolves options without connecting; -F isolates the test from personal SSH configuration.
    result = subprocess.run(
        ["ssh", "-G", "-F", str(ssh_config), *command[1:]],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = dict(
        line.split(" ", 1) for line in result.stdout.splitlines() if " " in line
    )
    assert fields["hostname"] == "127.0.0.1"
    assert fields["user"] == ("yaml-user" if overrides else "config-user")
    assert fields["port"] == ("3333" if overrides else "2222")
    assert fields["identityfile"] == str(identity)
    assert fields["batchmode"] == "yes"
    assert fields["stricthostkeychecking"] in ("yes", "true")
    assert cfg.servers[0].name == ("explicit-name" if overrides else "my-ssh-alias")


def test_explicit_identity_is_passed_to_ssh(tmp_path):
    identity = tmp_path / "identity"
    identity.write_text("test-only placeholder")
    cfg = config(servers=[{"host": "my-ssh-alias", "identity_file": str(identity)}])
    command = TunnelManager(cfg).command(cfg.servers[0])
    assert command[command.index("-i") + 1] == str(identity)


@pytest.mark.parametrize(
    "servers",
    [
        [{"host": "same-alias"}, {"host": "same-alias"}],
        [{"host": "first", "name": "second"}, {"host": "second"}],
    ],
)
def test_defaulted_names_must_be_unique(servers):
    with pytest.raises(ValidationError, match="unique"):
        config(servers=servers)


def ready_manager(cfg, monkeypatch):
    resumed = TunnelManager(cfg)
    monkeypatch.setattr(resumed, "_ensure_socket_free", lambda: None)
    monkeypatch.setattr(resumed, "_ready", AsyncMock(return_value=True))
    return resumed


async def test_state_resumes_last_successful_server_after_reordering(
    manager,
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "private" / "state.json"
    manager.config.state_file = path
    await manager.rotate()
    saved_connection = await manager.rotate()
    assert json.loads(path.read_text()) == {
        "version": 1,
        "server": "two",
        "host": "two.example",
    }
    assert path.stat().st_mode & 0o777 == 0o600
    await manager.close()
    assert path.exists()
    cfg = config(state_file=path, servers=list(reversed(manager.config.servers)))
    resumed = ready_manager(cfg, monkeypatch)
    try:
        status = await resumed.rotate()
        assert status["server"] == "two"
        assert status["connection_id"] != saved_connection["connection_id"]
        assert status["started_at"] >= saved_connection["started_at"]
        assert (await resumed.rotate())["server"] == "one"
    finally:
        await resumed.close()


async def test_failed_rotation_keeps_saved_server(manager, tmp_path):
    path = tmp_path / "state.json"
    manager.config.state_file = path
    await manager.rotate()
    original = path.read_bytes()
    manager._ready = AsyncMock(return_value=False)
    manager.config.startup_timeout_seconds = 0.01
    with pytest.raises(TunnelFailure):
        await manager.rotate()
    assert path.read_bytes() == original


async def test_atomic_state_write_failure_preserves_previous_file(
    manager,
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "state.json"
    manager.config.state_file = path
    await manager.rotate()
    original = path.read_bytes()

    def fail(*args):
        raise PermissionError("State directory is not writable")

    monkeypatch.setattr("tunnel_switch.state.os.replace", fail)
    with pytest.raises(TunnelFailure, match="not writable"):
        await manager.rotate()
    assert path.read_bytes() == original
    assert manager.process is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


@pytest.mark.parametrize(
    "saved",
    [None, {"version": 1, "server": "removed", "host": "removed.example"}],
)
async def test_missing_or_removed_state_starts_first_server(
    manager,
    tmp_path,
    monkeypatch,
    saved,
):
    path = tmp_path / "state.json"
    if saved:
        path.write_text(json.dumps(saved))
    resumed = ready_manager(config(state_file=path), monkeypatch)
    try:
        assert (await resumed.rotate())["server"] == "one"
    finally:
        await resumed.close()


@pytest.mark.parametrize(
    "contents",
    ["not JSON", '{"version": 2, "server": "one", "host": "one.example"}'],
)
def test_invalid_state_fails_without_overwriting(tmp_path, contents):
    path = tmp_path / "state.json"
    path.write_text(contents)
    settings = config(state_file=path)
    with pytest.raises(ValueError, match="Invalid tunnel state file"):
        TunnelManager(settings)
    assert path.read_text() == contents


async def test_server_name_overrides_saved_selection(manager, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("invalid state deliberately overridden")
    resumed = ready_manager(config(state_file=path, server_name="two"), monkeypatch)
    try:
        assert (await resumed.rotate())["server"] == "two"
        assert json.loads(path.read_text())["server"] == "two"
        assert (await resumed.rotate())["server"] == "one"
    finally:
        await resumed.close()


def test_state_paths_and_default_directory(tmp_path):
    from pathlib import Path

    path = tmp_path / "config.yaml"
    path.write_text("servers:\n  - host: one\nproxy: {}\n")
    assert (
        load_config(path).state_file
        == Path.home() / ".config/ssh-tunnel-switch/state.json"
    )
    path.write_text(path.read_text() + "state_file: data/last.json\n")
    assert load_config(path).state_file == tmp_path / "data/last.json"
    path.write_text("servers:\n  - host: one\nproxy: {}\nstate_file: null\n")
    assert load_config(path).state_file is None
    path.write_text("servers:\n  - host: one\nproxy: {}\nstate_file: config.yaml\n")
    with pytest.raises(ValueError, match="must differ"):
        load_config(path)


def test_state_cli_options_and_invalid_name(tmp_path, monkeypatch):

    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config().model_dump(mode="json")))
    captured = []
    monkeypatch.setattr(
        "tunnel_switch.cli.create_app",
        lambda cfg, token, manager=None, **kwargs: captured.append(cfg) or object(),
    )
    monkeypatch.setattr("tunnel_switch.cli.uvicorn.run", lambda *args, **kwargs: None)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "serve",
            "--config",
            str(path),
            "--state-file",
            str(tmp_path / "cli.json"),
            "--server-name",
            "two",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[0].state_file == tmp_path / "cli.json"
    assert captured[0].server_name == "two"
    assert str(tmp_path / "cli.json") in result.output
    captured.clear()
    assert (
        runner.invoke(
            app,
            ["serve", "--config", str(path), "--server-name", "missing"],
        ).exit_code
        != 0
    )
    assert not captured
    help_text = unstyle(runner.invoke(app, ["serve", "--help"]).output)
    assert "--state-file" in help_text
    assert "--server-name" in help_text
    with pytest.raises(ValidationError, match="server_name"):
        config(server_name="missing")


def test_version_flag_reports_the_package_version():

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"tunnel-switch {tunnel_switch.__version__}"


def test_startup_echo_reports_the_version(capsys):
    from tunnel_switch.cli import _echo_startup

    _echo_startup(config(), "token", None)
    assert f"Version: {__version__}" in capsys.readouterr().out


async def test_status_endpoint_reports_the_proxy_request_counter(manager):
    await manager.rotate()
    application = create_app(manager.config, "secret", manager)
    # The proxy records into the very counter the endpoint reports, so a
    # request carried over the tunnel shows up without any extra plumbing.
    assert application.state.http_proxy.counter is manager.counter
    application.state.http_proxy.counter.record()
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        payload = (await client.get("/status", headers=headers)).json()
    assert payload["requests"] == {"current": 1, "last": 0}
    await manager.close()


async def test_proxy_records_only_while_the_adopted_tunnel_runs(manager):
    await manager.rotate()
    application = create_app(manager.config, "secret", manager)
    # The gate is the manager's own check, so what answers on the SOCKS port
    # is counted only while the tunnel this manager adopted serves it.
    proxy = application.state.http_proxy
    assert proxy.active() is True
    manager.process.returncode = 255
    assert proxy.active() is False
    await manager.close()


def test_create_app_refuses_a_manager_beside_a_separate_counter(manager):
    # The proxy records into the manager's counter, so a second counter would
    # silently collect nothing: say so instead of reporting wrong totals.
    duplicate = RequestCounter()
    with pytest.raises(ValueError, match="not both"):
        create_app(manager.config, "secret", manager, counter=duplicate)


def test_create_app_gives_a_supplied_counter_to_the_manager_it_builds():
    counter = RequestCounter()
    application = create_app(config(), "secret", counter=counter)
    assert application.state.manager.counter is counter
    assert application.state.http_proxy.counter is counter
    assert application.state.http_proxy.active == application.state.manager.adopted


async def test_status_reports_the_package_version(manager):

    application = create_app(manager.config, "secret", manager)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        payload = (await client.get("/status", headers=headers)).json()
    assert payload["version"] == tunnel_switch.__version__
    await manager.close()


def test_package_version_falls_back_without_metadata(monkeypatch):

    import tunnel_switch

    def missing(_name):
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(tunnel_switch, "version", missing)
    assert tunnel_switch._package_version() == "0+unknown"
    monkeypatch.setattr(tunnel_switch, "version", lambda _name: "9.9.9")
    assert tunnel_switch._package_version() == "9.9.9"


def test_reported_version_matches_pyproject():
    """A stale editable install reports an old build: refresh it if this fails."""

    import tunnel_switch

    if tunnel_switch.__version__ == "0+unknown":
        pytest.skip("no distribution metadata (bare source tree)")
    declared = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(),
    )["project"]["version"]
    assert tunnel_switch.__version__ == declared, (
        "installed metadata is stale; refresh with `uv pip install -e .`"
    )


async def test_cancelled_state_save_finishes_before_next_rotation(
    manager,
    tmp_path,
    monkeypatch,
):
    import threading

    entered, release = threading.Event(), threading.Event()
    path = tmp_path / "state.json"
    manager.config.state_file = path

    def slow_save(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release state save")
        save_last_server(*args)

    monkeypatch.setattr("tunnel_switch.tunnel.save_last_server", slow_save)
    task = asyncio.create_task(manager.rotate())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(RotationRejected, match="in progress"):
            await manager.rotate()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert manager.process is None
    monkeypatch.setattr("tunnel_switch.tunnel.save_last_server", save_last_server)
    try:
        assert (await manager.rotate())["server"] == "two"
        assert json.loads(path.read_text())["server"] == "two"
    finally:
        await manager.close()


def test_cli_config_discovery(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "config.yaml"
    local.write_text(json.dumps(config().model_dump(mode="json")))
    assert resolve_config(None) == Path("config.yaml")
    assert CliRunner().invoke(app, ["validate"]).exit_code == 0
    preferred = tmp_path / "home/.config/ssh-tunnel-switch/config.yaml"
    preferred.parent.mkdir(parents=True)
    preferred.write_text(local.read_text())
    local.write_text("invalid local configuration")
    assert resolve_config(None) == preferred
    assert CliRunner().invoke(app, ["validate"]).exit_code == 0
    assert CliRunner().invoke(app, ["validate", "--config", str(local)]).exit_code != 0
    preferred.write_text("invalid preferred configuration")
    local.write_text(json.dumps(config().model_dump(mode="json")))
    assert CliRunner().invoke(app, ["validate"]).exit_code != 0
    preferred.unlink()
    local.unlink()
    assert CliRunner().invoke(app, ["validate"]).exit_code != 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
def test_state_syncs_directory_after_replace(tmp_path, monkeypatch):
    import os

    from tunnel_switch.state import save_last_server

    events = []
    fsync, replace = os.fsync, os.replace

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        fsync(fd)

    def rename(source, target):
        events.append("replace")
        replace(source, target)

    monkeypatch.setattr("tunnel_switch.state.os.fsync", sync)
    monkeypatch.setattr("tunnel_switch.state.os.replace", rename)
    save_last_server(tmp_path / "state.json", config().servers[0])
    assert events == ["file", "replace", "directory"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
def test_state_directory_sync_failure_closes_handle(tmp_path, monkeypatch):
    import os

    from tunnel_switch.state import save_last_server

    fsync = os.fsync
    directories = []

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(fd)
            raise OSError("directory sync failed")
        fsync(fd)

    monkeypatch.setattr("tunnel_switch.state.os.fsync", sync)
    server = config().servers[0]
    with pytest.raises(OSError, match="directory sync failed"):
        save_last_server(tmp_path / "state.json", server)
    assert len(directories) == 2
    with pytest.raises(OSError):
        os.fstat(directories[0])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
async def test_failed_directory_sync_restores_saved_selection(
    manager,
    tmp_path,
    monkeypatch,
):

    path = tmp_path / "state.json"
    manager.config.state_file = path
    await manager.rotate()
    previous = path.read_bytes()
    fsync = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        fsync(fd)

    monkeypatch.setattr("tunnel_switch.state.os.fsync", sync)
    with pytest.raises(TunnelFailure, match="directory sync failed"):
        await manager.rotate()
    assert manager.process is None
    assert path.read_bytes() == previous
    assert load_last_server(path, manager.config.servers) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_init_config_is_private_and_does_not_overwrite(tmp_path):
    path = tmp_path / "config.yaml"
    result = CliRunner().invoke(app, ["init-config", "--output", str(path)])
    assert result.exit_code == 0, result.output
    assert path.read_text() == Path("examples/config.yaml").read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    result = CliRunner().invoke(app, ["init-config", "--output", str(path)])
    assert result.exit_code != 0
    assert path.read_bytes() == original
    assert CliRunner().invoke(app, ["validate", "--config", str(path)]).exit_code == 0


@pytest.mark.parametrize("timeout_stage", ["connect", "read"])
async def test_readiness_retries_after_transient_probe_timeout(
    monkeypatch,
    timeout_stage,
):

    manager = TunnelManager(config(startup_timeout_seconds=2))
    manager.process = Process()
    reader = Mock()
    reader.readexactly = AsyncMock(
        side_effect=[TimeoutError(), b"\x05\x00"]
        if timeout_stage == "read"
        else [b"\x05\x00"],
    )
    writer = Mock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    connections = (
        [TimeoutError(), (reader, writer)]
        if timeout_stage == "connect"
        else [(reader, writer), (reader, writer)]
    )
    open_connection = AsyncMock(side_effect=connections)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    await manager._wait_until_ready()

    assert open_connection.await_count == 2
    assert writer.wait_closed.await_count == (1 if timeout_stage == "connect" else 2)
    assert manager.process.returncode is None


async def wait_for_condition(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def start_recovery(manager, pre_connect=None):
    manager.config.reconnect_delay_seconds = 0.01
    manager.config.reconnect_max_delay_seconds = 0.04
    manager.start_monitor(pre_connect, interval=0.005)


async def test_auto_reconnect_retries_current_server_without_cooldown(manager):
    manager.config.cooldown_seconds = 30
    first = await manager.select_server("two")
    manager.counter.record()
    start_recovery(manager)
    try:
        manager.process.returncode = 255
        await wait_for_condition(lambda: manager.adopted())
        recovered = manager.status()
        assert recovered["server"] == "two"
        assert recovered["connection_id"] != first["connection_id"]
        assert recovered["requests"] == {"current": 0, "last": 1}
        assert recovered["last_error"] is None
    finally:
        await manager.close()


async def test_auto_reconnect_fails_over_and_applies_pending_bind(manager, monkeypatch):
    await manager.rotate()
    spawn = AsyncMock(side_effect=[exited_ssh(), Process()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    pending = []

    def apply_bind():
        assert manager.busy
        manager.config.proxy.port = 1081
        pending.append(True)

    start_recovery(manager, apply_bind)
    try:
        manager.process.returncode = 255
        await wait_for_condition(manager.adopted)
        assert manager.status()["server"] == "two"
        assert spawn.await_count == 2
        assert pending == [True]
        assert "127.0.0.1:1081" in spawn.call_args.args
    finally:
        await manager.close()


async def test_auto_reconnect_backoff_is_capped_and_survives_outage(
    manager,
    monkeypatch,
    caplog,
):
    await manager.rotate()
    spawn = AsyncMock(side_effect=[exited_ssh() for _ in range(8)] + [Process()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    start_recovery(manager)
    try:
        manager.process.returncode = 255
        await wait_for_condition(manager.adopted)
        assert spawn.await_count == 9
        retries = [
            record.args[0]
            for record in caplog.records
            if record.msg.startswith("Tunnel reconnection failed")
        ]
        assert retries == [0.01, 0.02, 0.04, 0.04]
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "reloaded_delay,reloaded_cap,next_delay",
    [(0.02, 0.02, 0.02), (0.25, 4, 0.5), (2, 4, 4)],
)
async def test_reload_retry_delays_preserves_deadline_then_uses_current_values(
    manager,
    monkeypatch,
    tmp_path,
    caplog,
    reloaded_delay,
    reloaded_cap,
    next_delay,
):
    """Preserve the pending wait and derive later waits from reloaded settings."""
    await manager.rotate()
    manager.config.logging.file = None
    manager.config.reconnect_delay_seconds = 1
    manager.config.reconnect_max_delay_seconds = 4
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(manager.config.model_dump(mode="json")))
    application = create_app(manager.config, "secret", manager, config_path=path)
    attempts = []
    clock = [0.0]
    monkeypatch.setattr(
        "tunnel_switch.tunnel.time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    async def fail(_pre_connect):
        attempts.append(clock[0])
        raise TunnelFailure("Temporary network outage")

    monkeypatch.setattr(manager, "_recover", fail)
    manager.start_monitor(interval=0.005)
    manager.process.returncode = 255
    try:
        await wait_for_condition(lambda: len(attempts) == 1)
        desired = manager.config.model_dump(mode="json")
        desired.update(
            reconnect_delay_seconds=reloaded_delay,
            reconnect_max_delay_seconds=reloaded_cap,
        )
        path.write_text(json.dumps(desired))
        outcome = await application.state.reloader.reload_now()
        assert outcome.applied
        await asyncio.sleep(0.03)
        assert len(attempts) == 1
        clock[0] = 1
        await wait_for_condition(lambda: len(attempts) >= 2)
        retries = [
            record.args[0]
            for record in caplog.records
            if record.msg.startswith("Tunnel reconnection failed")
        ]
        assert retries[0] == 1
        assert retries[1] == next_delay
        assert attempts[1] - attempts[0] == 1
        clock[0] = 1 + next_delay / 2
        await asyncio.sleep(0.03)
        assert len(attempts) == 2
        clock[0] = 1 + next_delay + 0.01
        await wait_for_condition(lambda: len(attempts) == 3)
    finally:
        await manager.close()


async def test_proxy_admission_is_disabled_before_pending_bind_is_published(
    manager,
    monkeypatch,
    tmp_path,
):
    """Do not admit traffic on a new bind while the old SSH process stops."""
    await manager.rotate()
    manager.config.logging.file = None
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(manager.config.model_dump(mode="json")))
    application = create_app(manager.config, "secret", manager, config_path=path)
    desired = manager.config.model_dump(mode="json")
    desired["proxy"]["port"] = 1081
    path.write_text(json.dumps(desired))
    await application.state.reloader.reload_now()
    entered, release = asyncio.Event(), asyncio.Event()
    stop = manager._stop

    async def slow_stop():
        entered.set()
        await release.wait()
        await stop()

    monkeypatch.setattr(manager, "_stop", slow_stop)
    open_connection = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    task = asyncio.create_task(
        manager.rotate(application.state.reloader.apply_pending_binds)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.process.returncode is None
        assert application.state.http_proxy.config.proxy.port == 1081
        assert not manager.adopted()
        assert manager.connection_id() is None
        with pytest.raises(ProxyError, match="Managed tunnel unavailable"):
            await application.state.http_proxy._socks("example.test", 80)
        open_connection.assert_not_awaited()
        release.set()
        await task
        assert manager.adopted()
    finally:
        release.set()
        try:
            await task
        finally:
            await manager.close()


async def test_auto_reconnect_respects_disconnect_and_manual_reconnect(
    manager,
    monkeypatch,
):
    await manager.rotate()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), exited_ssh(), Process(), Process()]),
    )
    start_recovery(manager)
    manager.config.reconnect_delay_seconds = 0.2
    manager.config.reconnect_max_delay_seconds = 0.2
    try:
        manager.process.returncode = 255
        await wait_for_condition(lambda: manager.last_error is not None)
        await manager.disconnect()
        await asyncio.sleep(0.25)
        assert manager.process is None
        assert (await manager.restart())["server"] == "two"
        manager.process.returncode = 255
        await wait_for_condition(manager.adopted)
        assert manager.status()["server"] == "two"
    finally:
        await manager.close()


async def test_auto_reconnect_does_not_start_an_intentionally_idle_service(manager):
    start_recovery(manager)
    try:
        await asyncio.sleep(0.08)
        assert manager.process is None
        assert not manager._attempted
    finally:
        await manager.close()


async def test_auto_reconnect_can_be_disabled_and_reenabled_live(manager):
    await manager.rotate()
    manager.config.reconnect_attempts = 0
    start_recovery(manager)
    try:
        dead = manager.process
        dead.returncode = 255
        await asyncio.sleep(0.08)
        assert manager.process is dead
        manager.config.reconnect_attempts = 5
        await wait_for_condition(manager.adopted)
    finally:
        await manager.close()


async def test_monitor_reports_ssh_exit_once_when_recovery_disabled(manager, caplog):
    await manager.rotate()
    manager.config.reconnect_attempts = 0
    start_recovery(manager)
    try:
        dead = manager.process
        dead.returncode = 255
        await wait_for_condition(lambda: manager.last_error is not None)
        await asyncio.sleep(0.08)
        assert manager.process is dead
        assert manager.last_error == "SSH tunnel exited with code 255"
        exits = [
            record
            for record in caplog.records
            if record.getMessage() == manager.last_error
        ]
        assert len(exits) == 1
        assert not any(
            "reconnecting" in record.getMessage() for record in caplog.records
        )
        assert not manager.busy
    finally:
        await manager.close()


async def test_failed_selection_allows_subsequent_auto_recovery_failover(
    manager,
    monkeypatch,
):
    spawn = AsyncMock(side_effect=[exited_ssh(), exited_ssh(), Process()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    # The selection itself must fail without trying server one.
    with pytest.raises(TunnelFailure):
        await manager.select_server("two")
    assert spawn.await_count == 1
    start_recovery(manager)
    try:
        await wait_for_condition(manager.adopted)
        assert manager.status()["server"] == "one"
        assert spawn.await_count == 3
        assert spawn.call_args_list[1].args[-1] == "two.example"
        assert spawn.call_args_list[2].args[-1] == "one.example"
    finally:
        await manager.close()


async def test_auto_reconnect_resolves_server_identity_after_reload(manager):
    await manager.rotate()
    manager.config = manager.config.model_copy(deep=True)
    manager.config.servers.reverse()
    start_recovery(manager)
    try:
        manager.process.returncode = 255
        await wait_for_condition(manager.adopted)
        assert manager.status()["server"] == "one"
    finally:
        await manager.close()


async def test_manual_selection_supersedes_a_pending_auto_reconnect(
    manager,
    monkeypatch,
):
    await manager.rotate()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), exited_ssh(), Process()]),
    )
    start_recovery(manager)
    manager.config.reconnect_delay_seconds = 0.2
    manager.config.reconnect_max_delay_seconds = 0.2
    try:
        manager.process.returncode = 255
        await wait_for_condition(lambda: manager.last_error is not None)
        selected = await manager.select_server("two")
        await asyncio.sleep(0.25)
        assert manager.status()["connection_id"] == selected["connection_id"]
        assert manager.status()["server"] == "two"
    finally:
        await manager.close()


async def test_auto_reconnect_attempts_immediately_before_backoff(manager):
    await manager.rotate()
    # A long backoff must not delay the first attempt after either disconnection.
    manager.config.reconnect_delay_seconds = 60
    manager.config.reconnect_max_delay_seconds = 60
    manager.start_monitor(interval=0.005)
    try:
        for _ in range(2):
            previous = manager.status()["connection_id"]
            manager.process.returncode = 255
            async with asyncio.timeout(1):
                await wait_for_condition(manager.adopted)
            assert manager.status()["connection_id"] != previous
            assert manager.status()["server"] == "one"
    finally:
        await manager.close()


@pytest.mark.parametrize("budget", [0, 1, 5])
async def test_auto_reconnect_limits_attempts_per_outage(
    manager,
    monkeypatch,
    caplog,
    budget,
):
    await manager.rotate()
    manager.config.reconnect_attempts = budget
    spawn = AsyncMock(side_effect=lambda *args, **kwargs: exited_ssh())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    start_recovery(manager)
    manager.process.returncode = 255
    try:
        if budget:
            await wait_for_condition(
                lambda: spawn.await_count == budget * 2 and not manager.busy,
            )
        await asyncio.sleep(0.15)
        assert spawn.await_count == budget * 2
        assert manager.status()["state"] == "disconnected"
        exhausted = [
            record for record in caplog.records if "exhausted" in record.getMessage()
        ]
        assert len(exhausted) == (1 if budget else 0)
    finally:
        await manager.close()


async def test_manual_connection_resets_exhausted_recovery_budget(manager, monkeypatch):
    await manager.rotate()
    manager.config.reconnect_attempts = 1
    spawn = AsyncMock(side_effect=[exited_ssh(), exited_ssh(), Process(), Process()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    start_recovery(manager)
    manager.process.returncode = 255
    try:
        await wait_for_condition(lambda: spawn.await_count == 2 and not manager.busy)
        await manager.select_server("one")
        manager.process.returncode = 255
        await wait_for_condition(manager.adopted)
        assert spawn.await_count == 4
    finally:
        await manager.close()


async def test_live_retry_budget_increase_resumes_exhausted_recovery(
    manager,
    monkeypatch,
):
    await manager.rotate()
    manager.config.reconnect_attempts = 1
    spawn = AsyncMock(side_effect=[exited_ssh(), exited_ssh(), Process()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    start_recovery(manager)
    manager.process.returncode = 255
    try:
        await wait_for_condition(lambda: spawn.await_count == 2 and not manager.busy)
        manager.config.reconnect_attempts = 2
        await wait_for_condition(manager.adopted)
        assert spawn.await_count == 3
    finally:
        await manager.close()


async def test_auto_reconnect_waits_for_a_busy_manual_change(manager):
    await manager.rotate()
    start_recovery(manager)
    entered, release = asyncio.Event(), asyncio.Event()

    async def ready():
        entered.set()
        await release.wait()
        return True

    manager._ready = ready
    task = asyncio.create_task(manager.select_server("two"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        process = manager.process
        await asyncio.sleep(0.08)
        assert manager.process is process
        assert not process.terminated
        release.set()
        assert (await task)["server"] == "two"
    finally:
        release.set()
        await task
        await manager.close()


async def test_close_cancels_an_in_progress_auto_reconnect(manager):
    await manager.rotate()
    entered = asyncio.Event()

    async def ready():
        entered.set()
        await asyncio.Event().wait()

    manager._ready = ready
    start_recovery(manager)
    task = manager._monitor_task
    manager.process.returncode = 255
    try:
        await asyncio.wait_for(entered.wait(), 2)
        process = manager.process
    finally:
        await manager.close()
    assert task.done()
    assert process.terminated
    assert manager.process is None
    assert not manager.busy


async def test_close_propagates_caller_cancellation_after_cleanup(manager):
    await manager.rotate()
    process = manager.process
    started, cancelling = asyncio.Event(), asyncio.Event()

    async def monitor():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await asyncio.Event().wait()

    manager._monitor_task = asyncio.create_task(monitor())
    await started.wait()
    close = asyncio.create_task(manager.close())
    try:
        await asyncio.wait_for(cancelling.wait(), 2)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert process.terminated
        assert manager.process is None
        assert manager._monitor_task is None
    finally:
        if not close.done():
            close.cancel()
            await asyncio.gather(close, return_exceptions=True)


async def test_close_completes_when_cancelled_recovery_state_save_fails(
    manager,
    monkeypatch,
    tmp_path,
    caplog,
):
    """A persistence failure must not turn shutdown cancellation into a retry."""
    await manager.rotate()
    entered, release = threading.Event(), threading.Event()
    manager.config.state_file = tmp_path / "state.json"

    def failed_save(*_args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release the state save")
        raise OSError("State save failed during shutdown")

    monkeypatch.setattr("tunnel_switch.tunnel.save_last_server", failed_save)
    start_recovery(manager)
    monitor = manager._monitor_task
    manager.process.returncode = 255
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        process = manager.process
        close = asyncio.create_task(manager.close())
        await wait_for_condition(lambda: monitor.cancelling() > 0)
        release.set()
        await asyncio.wait_for(close, 2)
        assert monitor.cancelled()
        assert process.terminated
        assert manager.process is None
        assert manager._monitor_task is None
        assert not manager.busy
        assert "Tunnel state save failed during cancellation" in caplog.text
    finally:
        release.set()
        if close is not None:
            if not close.done():
                close.cancel()
            await asyncio.gather(close, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("save_fails", [False, True])
async def test_cancelled_selection_state_save_stops_process(
    manager,
    monkeypatch,
    tmp_path,
    save_fails,
):
    """Selection cancellation must stop SSH regardless of persistence outcome."""
    entered, release = threading.Event(), threading.Event()
    manager.config.state_file = tmp_path / "state.json"

    def blocked_save(path, server):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release the state save")
        if save_fails:
            raise OSError("State save failed after selection cancellation")
        save_last_server(path, server)

    monkeypatch.setattr("tunnel_switch.tunnel.save_last_server", blocked_save)
    selection = asyncio.create_task(manager.select_server("two"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        process = manager.process
        selection.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await selection
        if save_fails:
            assert isinstance(cancelled.value.__cause__, OSError)
        assert process.terminated
        assert manager.process is None
        assert manager.connection_id() is None
        assert not manager.busy
    finally:
        release.set()
        if not selection.done():
            selection.cancel()
        await asyncio.gather(selection, return_exceptions=True)
        await manager.close()


async def test_close_reports_unexpected_monitor_failure_after_cleanup(manager):
    await manager.rotate()
    process = manager.process

    async def failed_monitor():
        raise RuntimeError("Monitor failed")

    manager._monitor_task = asyncio.create_task(failed_monitor())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="Monitor failed"):
        await manager.close()
    assert process.terminated
    assert manager.process is None


async def test_close_stops_ssh_when_recovery_state_save_stalls(
    manager,
    monkeypatch,
    tmp_path,
    caplog,
):
    """A stalled filesystem must not prevent the manager from stopping SSH."""
    await manager.rotate()
    entered, release = threading.Event(), threading.Event()
    manager.config.state_file = tmp_path / "state.json"
    manager.config.stop_timeout_seconds = 0.03

    def stalled_save(path, server):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release the state save")
        save_last_server(path, server)

    monkeypatch.setattr("tunnel_switch.tunnel.save_last_server", stalled_save)
    start_recovery(manager)
    monitor = manager._monitor_task
    manager.process.returncode = 255
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        process = manager.process
        await asyncio.wait_for(manager.close(), 1)
        assert not release.is_set()
        assert not monitor.done()
        assert process.terminated
        assert manager.process is None
        assert manager._monitor_task is None
        assert "Tunnel monitor did not stop in time; stopping SSH" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(monitor, return_exceptions=True), 2)
        await manager.close()


async def test_close_collects_late_monitor_failure(manager, caplog):
    """Report failures after the bounded shutdown wait without leaking task errors."""
    await manager.rotate()
    process = manager.process
    manager.config.stop_timeout_seconds = 0.03
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_failure():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("Late monitor failure") from None

    monitor = asyncio.create_task(delayed_failure())
    manager._monitor_task = monitor
    try:
        await entered.wait()
        await asyncio.wait_for(manager.close(), 1)
        assert not monitor.done()
        assert process.terminated
        assert manager.process is None
        release.set()
        await asyncio.gather(monitor, return_exceptions=True)
        await asyncio.sleep(0)
        assert "Tunnel monitor failed after shutdown" in caplog.text
        assert "Late monitor failure" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(monitor, return_exceptions=True), 2)
        await manager.close()


async def test_lifespan_keeps_api_up_after_initial_tunnel_failure(manager, monkeypatch):
    manager.config.start_on_launch = True
    spawn = AsyncMock(side_effect=[exited_ssh(), exited_ssh(), Process()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    manager.config.reconnect_delay_seconds = 0.01
    application = create_app(manager.config, "secret", manager)
    async with application.router.lifespan_context(application):
        assert manager.status()["state"] == "disconnected"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/status",
                headers={"Authorization": "Bearer secret"},
            )
            assert response.status_code == 200
        await wait_for_condition(manager.adopted)
    assert manager.process is None
    assert manager._monitor_task is None


async def test_lifespan_still_fails_startup_when_recovery_disabled(
    manager,
    monkeypatch,
):
    manager.config.start_on_launch = True
    manager.config.reconnect_attempts = 0
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=[exited_ssh(), exited_ssh()]),
    )
    application = create_app(manager.config, "secret", manager)
    lifespan = application.router.lifespan_context(application)
    with pytest.raises(TunnelFailure):
        await lifespan.__aenter__()
    assert manager.process is None


@pytest.mark.parametrize(
    "changes",
    [
        {"reconnect_delay_seconds": 0},
        {"reconnect_max_delay_seconds": 0},
        {"reconnect_delay_seconds": 10, "reconnect_max_delay_seconds": 5},
        {"reconnect_attempts": -1},
        {"reconnect_attempts": 1.5},
    ],
)
def test_reconnect_configuration_rejects_invalid_delays(changes):
    with pytest.raises(ValidationError):
        config(**changes)


def test_reconnect_attempt_budget_defaults_to_five():
    assert config().reconnect_attempts == 5
