"""Opt-in real OpenSSH tests; Docker fixtures never use personal SSH credentials."""
import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from uuid import uuid4

import pytest

from tunnel_switch.config import Config
from tunnel_switch.tunnel import TunnelFailure, TunnelManager

pytestmark = pytest.mark.ssh_integration


def docker(*args, timeout=60):
    return subprocess.run(["docker", *args], check=True, capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_ssh(port):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
                if sock.recv(128).startswith(b"SSH-"):
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise AssertionError("Disposable SSH server did not become ready")


@pytest.fixture
def ssh_servers(tmp_path_factory):
    if os.environ.get("TUNNEL_SWITCH_TEST_SSH") != "1":
        pytest.skip("Set TUNNEL_SWITCH_TEST_SSH=1 to run disposable Docker SSH tests")
    # Explicit opt-in must fail if Docker is unavailable; CI must not silently skip.
    for command in ("docker", "ssh", "ssh-keygen"):
        assert shutil.which(command), f"Required integration tool missing: {command}"
    docker("info", "--format", "{{.ServerVersion}}")
    directory = tmp_path_factory.mktemp("real-ssh")
    key = directory / "identity"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    image = f"tunnel-switch-test:{uuid4().hex}"
    containers = []
    image_ready = False
    try:
        docker("build", "-q", "-t", image, str(Path(__file__).parent / "fixtures/ssh"), timeout=300)
        image_ready = True
        records = []
        for name in ("one", "two"):
            container = docker("run", "-d", "--label", "tunnel-switch.integration=true",
                               "-p", "127.0.0.1::22", "-e", f"TUNNEL_TEST_NAME={name}", image)
            containers.append(container)
            docker("cp", str(key.with_suffix(".pub")), f"{container}:/tmp/test_authorized_keys")
            port = int(docker("port", container, "22/tcp").rsplit(":", 1)[1])
            wait_for_ssh(port)
            public = docker("exec", container, "cat", "/tmp/ssh_host_ed25519_key.pub")
            records.append({"name": name, "port": port, "public": public, "container": container})
        known_hosts = directory / "known_hosts"
        known_hosts.write_text("".join(f"[127.0.0.1]:{r['port']} {r['public']}\n" for r in records))
        ssh_config = directory / "ssh_config"
        ssh_config.write_text("".join(
            f"Host {r['name']}\n  HostName 127.0.0.1\n  Port {r['port']}\n"
            f"  User tunnel\n  IdentityFile {key}\n  IdentitiesOnly yes\n"
            f"  IdentityAgent none\n  UserKnownHostsFile {known_hosts}\n"
            "  GlobalKnownHostsFile /dev/null\n"
            for r in records))
        yield ssh_config, known_hosts, records
    finally:
        # Remove only resources owned by this fixture, including after failed setup.
        errors = []
        for container in reversed(containers):
            try:
                docker("rm", "-f", container)
            except Exception as exc:
                errors.append(exc)
        if image_ready:
            try:
                docker("image", "rm", "-f", image)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("SSH fixture cleanup failed", errors)


def manager_for(ssh_servers, monkeypatch):
    ssh_config, _, _ = ssh_servers
    cfg = Config.model_validate({
        "servers": [{"host": "one"}, {"host": "two"}],
        "proxy": {"ip": "127.0.0.1", "port": free_port()},
        "http_proxy": {"enabled": False}, "state_file": None, "cooldown_seconds": 0,
        "startup_timeout_seconds": 5, "stop_timeout_seconds": 2,
    })
    manager = TunnelManager(cfg)
    command = manager.command
    # Run the production OpenSSH command, isolating only its config file from ~/.ssh.
    monkeypatch.setattr(manager, "command", lambda server: ["ssh", "-F", str(ssh_config), *command(server)[1:]])
    return manager


async def forwarded_body(manager):
    reader, writer = await asyncio.open_connection("127.0.0.1", manager.config.proxy.port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        writer.write(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + (8080).to_bytes(2, "big"))
        await writer.drain()
        reply = await reader.readexactly(4)
        assert reply[:3] == b"\x05\x00\x00"
        length = {1: 4, 4: 16}.get(reply[3])
        if reply[3] == 3:
            length = (await reader.readexactly(1))[0]
        assert length is not None
        await reader.readexactly(length + 2)
        writer.write(b"GET / HTTP/1.0\r\nHost: fixture\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert response.startswith(b"HTTP/1.0 200")
        return response.split(b"\r\n\r\n", 1)[1].strip()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_real_ssh_rotation_forwarding_and_cleanup(ssh_servers, monkeypatch):
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        first = await manager.rotate()
        old = manager.process
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"one"
        second = await manager.rotate()
        assert old.returncode is not None
        assert first["connection_id"] != second["connection_id"]
        assert second["started_at"] >= first["started_at"]
        assert second["server"] == "two"
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"two"
        current = manager.process
    finally:
        await manager.close()
    assert current.returncode is not None
    assert manager.status()["state"] == "disconnected"
    # Cleanup releases the actual SOCKS listening socket.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", manager.config.proxy.port))


async def test_real_ssh_rejects_unknown_host_then_recovers(ssh_servers, monkeypatch):
    _, known_hosts, records = ssh_servers
    original = known_hosts.read_text()
    # Trust only server two: server one's key must be rejected, never accepted.
    known_hosts.write_text(f"[127.0.0.1]:{records[1]['port']} {records[1]['public']}\n")
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        with pytest.raises(TunnelFailure, match="SSH exited"):
            await manager.rotate()
        assert manager.process is None
        assert manager.status()["connection_id"] is None
        assert (await manager.rotate())["server"] == "two"
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"two"
    finally:
        await manager.close()
        known_hosts.write_text(original)


async def test_real_ssh_server_loss_and_recovery(ssh_servers, monkeypatch):
    records = ssh_servers[2]
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        await manager.rotate()
        process = manager.process
        await asyncio.to_thread(docker, "stop", "--time", "1", records[0]["container"])
        await asyncio.wait_for(process.wait(), 10)
        status = manager.status()
        assert status["state"] == "disconnected"
        assert status["connection_id"] is None and status["started_at"] is None
        assert (await manager.rotate())["server"] == "two"
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"two"
    finally:
        await manager.close()
