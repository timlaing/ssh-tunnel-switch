import asyncio
import json
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
    return Config.model_validate({"servers": [{"name": "one", "host": "one.example", "user": "tunnel"},
                                               {"name": "two", "host": "two.example", "user": "tunnel"}],
                                  "proxy": {"ip": "127.0.0.1", "port": 1080},
                                  "start_on_launch": False, "cooldown_seconds": 0, "http_proxy": {"enabled": False}, **changes})


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
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **kw: Process()))
    return manager


async def test_rotation_stops_old_and_changes_id(manager):
    first = await manager.rotate()
    process = manager.process
    second = await manager.rotate()
    assert process.terminated
    assert first["connection_id"] != second["connection_id"]
    assert first["server"] == "one" and second["server"] == "two"
    assert second["started_at"].endswith("+00:00")
    manager.process.returncode = 255
    assert manager.status()["connection_id"] is None
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/status")).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert (await client.post("/rotate", headers=headers)).status_code == 200
        manager._busy = True
        response = await client.post("/rotate", headers=headers)
        assert response.status_code == 429 and response.headers["Retry-After"] == "1"
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
    monkeypatch.setattr("sys.argv", ["tunnel-switch", "validate", "--config", "/missing/config.yaml"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    text = capsys.readouterr().err
    assert "Error:" in text and "Usage:" in text and "--help" in text


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
    script.write_text('''import asyncio, sys
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
''')
    manager = TunnelManager(config(proxy={"ip": "127.0.0.1", "port": port}))
    monkeypatch.setattr(manager, "command", lambda server: [sys.executable, str(script), str(port)])
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
def test_startup_generates_persists_and_prints_token(tmp_path, monkeypatch, capsys, token_line):
    from tunnel_switch.cli import serve, settings
    path = tmp_path / "config.yaml"
    path.write_text("# Keep this comment\nservers:\n  - name: one\n    host: one.example\n    user: tunnel\nproxy:\n  ip: 127.0.0.1\n  port: 1080\napi:\n  port: 8765 # API port\n" + token_line)
    run = []
    monkeypatch.setattr("tunnel_switch.cli.uvicorn.run", lambda *args, **kwargs: run.append(kwargs))
    serve(path)
    saved = load_config(path).api.token
    assert saved and len(saved) >= 40
    assert f"API token: {saved}" in capsys.readouterr().out
    assert "# Keep this comment" in path.read_text() and "# API port" in path.read_text()
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
    path.write_text("servers:\n  - host: my-ssh-alias\nproxy:\n  ip: 127.0.0.1\n  port: 1080\n")
    cfg = load_config(path)
    server = cfg.servers[0]
    assert server.name == "my-ssh-alias"
    assert server.user is None and server.port is None and server.identity_file is None
    command = TunnelManager(cfg).command(server)
    assert "-l" not in command and "-p" not in command and "-i" not in command
    assert "-F" not in command
    assert command[-2:] == ["--", "my-ssh-alias"]


@pytest.mark.parametrize("overrides", [False, True])
def test_ssh_resolves_config_and_explicit_overrides(tmp_path, overrides):
    import subprocess
    ssh_config = tmp_path / "ssh_config"
    identity = tmp_path / "identity"
    ssh_config.write_text(f"Host my-ssh-alias\n  HostName 127.0.0.1\n  User config-user\n  Port 2222\n  IdentityFile {identity}\n")
    document = {"host": "my-ssh-alias"}
    if overrides:
        document.update(name="explicit-name", user="yaml-user", port=3333)
    cfg = config(servers=[document])
    command = TunnelManager(cfg).command(cfg.servers[0])
    # -G resolves options without connecting; -F isolates the test from personal SSH configuration.
    result = subprocess.run(["ssh", "-G", "-F", str(ssh_config), *command[1:]],
                            check=True, capture_output=True, text=True)
    fields = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
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


@pytest.mark.parametrize("servers", [
    [{"host": "same-alias"}, {"host": "same-alias"}],
    [{"host": "first", "name": "second"}, {"host": "second"}],
])
def test_defaulted_names_must_be_unique(servers):
    with pytest.raises(ValidationError, match="unique"):
        config(servers=servers)
