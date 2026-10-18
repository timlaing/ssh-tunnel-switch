import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner
from tunnel_switch.api import create_app
from tunnel_switch.cli import app, main
from tunnel_switch.config import Config, load_config
from tunnel_switch.tunnel import TunnelManager, TunnelFailure, RotationRejected


def config(**changes):
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
        }
    )


class Process:
    returncode = None

    def __init__(self):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def manager(monkeypatch):
    manager = TunnelManager(config())
    monkeypatch.setattr(manager, "_ensure_socket_free", lambda: None)
    monkeypatch.setattr(manager, "_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=lambda *a, **kw: Process()),
    )
    return manager


async def test_rotation_stops_old_and_changes_id(manager):
    first = await manager.rotate()
    process = manager.process
    second = await manager.rotate()
    assert process.terminated
    assert first["connection_id"] != second["connection_id"]
    assert first["server"] == "one"
    assert second["server"] == "two"
    assert second["started_at"].endswith("+00:00")
    manager.process.returncode = 255
    assert manager.status()["connection_id"] is None
    await manager.close()


async def test_status_reports_one_connection_snapshot(manager):
    status = await manager.rotate()
    assert status["connection_id"]
    assert status["server"] == "one"
    # The trio is published and cleared as one snapshot, so the status display
    # thread can never read an ID paired with a missing server name.
    manager.process.returncode = 255
    dead = manager.status()
    assert (dead["connection_id"], dead["server"], dead["started_at"]) == (
        None,
        None,
        None,
    )
    await manager.close()


async def test_concurrent_rotation_rejected(manager):
    entered, release = asyncio.Event(), asyncio.Event()

    async def ready():
        entered.set()
        await release.wait()
        return True

    manager._ready = ready
    task = asyncio.create_task(manager.rotate())
    await entered.wait()
    with pytest.raises(RotationRejected, match="in progress"):
        await manager.rotate()
    assert manager.status()["state"] == "rotating"
    release.set()
    await task
    await manager.close()


async def test_failure_cleans_process_and_cools_down(manager):
    manager.config.cooldown_seconds = 30
    manager.config.startup_timeout_seconds = 0.01
    manager._ready = AsyncMock(return_value=False)
    with pytest.raises(TunnelFailure, match="timed out"):
        await manager.rotate()
    assert manager.process is None
    assert manager.status()["state"] == "disconnected"
    with pytest.raises(RotationRejected, match="cooldown"):
        await manager.rotate()


def exited_ssh(code=255):
    """A spawned ssh that failed to connect and exited, as ssh always does."""
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
    manager, monkeypatch
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
        transport=httpx.ASGITransport(app=app), base_url="http://test"
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
        transport=httpx.ASGITransport(app=app), base_url="http://test"
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
        "sys.argv", ["tunnel-switch", "validate", "--config", "/missing/config.yaml"]
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
    import socket
    import sys

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
        manager, "command", lambda server: [sys.executable, str(script), str(port)]
    )
    try:
        first = await manager.rotate()
        old = manager.process
        second = await manager.rotate()
        assert first["connection_id"] != second["connection_id"]
        assert old.returncode is not None
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
    tmp_path, monkeypatch, capsys, token_line
):
    from tunnel_switch.cli import serve, settings

    path = tmp_path / "config.yaml"
    path.write_text(
        "# Keep this comment\nservers:\n  - name: one\n    host: one.example\n    user: tunnel\nproxy:\n  ip: 127.0.0.1\n  port: 1080\napi:\n  port: 8765 # API port\n"
        + token_line
    )
    run = []
    monkeypatch.setattr(
        "tunnel_switch.cli.uvicorn.run", lambda *args, **kwargs: run.append(kwargs)
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
    from tunnel_switch.cli import serve

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
    from tunnel_switch.cli import serve, settings

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
        "servers:\n  - host: my-ssh-alias\nproxy:\n  ip: 127.0.0.1\n  port: 1080\nstate_file: null\n"
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
        f"Host my-ssh-alias\n  HostName 127.0.0.1\n  User config-user\n  Port 2222\n  IdentityFile {identity}\n"
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
    manager, tmp_path, monkeypatch
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
    manager, tmp_path, monkeypatch
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
    "saved", [None, {"version": 1, "server": "removed", "host": "removed.example"}]
)
async def test_missing_or_removed_state_starts_first_server(
    manager, tmp_path, monkeypatch, saved
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
    "contents", ["not JSON", '{"version": 2, "server": "one", "host": "one.example"}']
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
    from click import unstyle

    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config().model_dump(mode="json")))
    captured = []
    monkeypatch.setattr(
        "tunnel_switch.cli.create_app",
        lambda cfg, token, manager=None: captured.append(cfg) or object(),
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
            app, ["serve", "--config", str(path), "--server-name", "missing"]
        ).exit_code
        != 0
    )
    assert not captured
    help_text = unstyle(runner.invoke(app, ["serve", "--help"]).output)
    assert "--state-file" in help_text
    assert "--server-name" in help_text
    with pytest.raises(ValidationError, match="server_name"):
        config(server_name="missing")


async def test_cancelled_state_save_finishes_before_next_rotation(
    manager, tmp_path, monkeypatch
):
    import threading
    from tunnel_switch.state import save_last_server

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
    from tunnel_switch.cli import resolve_config

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
    import stat
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
    import stat
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
    manager, tmp_path, monkeypatch
):
    import stat
    from tunnel_switch.state import load_last_server

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
    monkeypatch, timeout_stage
):
    from unittest.mock import Mock

    manager = TunnelManager(config(startup_timeout_seconds=2))
    manager.process = Process()
    reader = Mock()
    reader.readexactly = AsyncMock(
        side_effect=[TimeoutError(), b"\x05\x00"]
        if timeout_stage == "read"
        else [b"\x05\x00"]
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
