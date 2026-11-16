"""Opt-in real OpenSSH tests; Docker fixtures never use personal SSH credentials."""

import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest

from tunnel_switch.config import Config
from tunnel_switch.tunnel import TunnelFailure, TunnelManager

pytestmark = pytest.mark.ssh_integration


class SSHRecord(TypedDict):
    """Describe one disposable server and the public host key learned from Docker."""

    name: str
    port: int
    public: str
    container: str


type SSHServers = tuple[Path, Path, list[SSHRecord]]


def executable_for(command: str) -> str:
    """Resolve required integration tools to absolute paths or fail explicit opt-in."""
    executable = shutil.which(command)
    if executable is None or not Path(executable).is_absolute():
        pytest.fail(f"Required absolute integration tool missing: {command}")
    return executable


def docker(*args: str, timeout: float = 60) -> str:
    """Run fixture-owned Docker operations with checked status and bounded runtime."""
    executable = executable_for("docker")
    return subprocess.run(
        [executable, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout.strip()


def free_port() -> int:
    """Reserve an ephemeral loopback port long enough to select a test listener."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_ssh(port: int) -> None:
    """Wait for the disposable server's SSH banner instead of just an open socket."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
                if sock.recv(128).startswith(b"SSH-"):
                    return
        except OSError:
            pass
        time.sleep(0.1)
    pytest.fail("Disposable SSH server did not become ready")


@pytest.fixture
def ssh_servers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SSHServers]:
    """Create two opt-in containers with disposable keys and clean up owned resources."""
    if os.environ.get("TUNNEL_SWITCH_TEST_SSH") != "1":
        pytest.skip("Set TUNNEL_SWITCH_TEST_SSH=1 to run disposable Docker SSH tests")
    # Explicit opt-in must fail if Docker is unavailable; CI must not silently skip.
    for command in ("docker", "ssh", "ssh-keygen"):
        executable_for(command)
    docker("info", "--format", "{{.ServerVersion}}")
    directory = tmp_path_factory.mktemp("real-ssh")
    key = directory / "identity"
    keygen = executable_for("ssh-keygen")
    subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    image = f"tunnel-switch-test:{uuid4().hex}"
    containers = []
    image_ready = False
    try:
        docker(
            "build",
            "-q",
            "-t",
            image,
            str(Path(__file__).parent / "fixtures/ssh"),
            timeout=300,
        )
        image_ready = True
        records: list[SSHRecord] = []
        for name in ("one", "two"):
            container = docker(
                "run",
                "-d",
                "--label",
                "tunnel-switch.integration=true",
                "-p",
                "127.0.0.1::22",
                "-e",
                f"TUNNEL_TEST_NAME={name}",
                image,
            )
            containers.append(container)
            docker(
                "cp",
                str(key.with_suffix(".pub")),
                f"{container}:/tmp/test_authorized_keys",
            )
            port = int(docker("port", container, "22/tcp").rsplit(":", 1)[1])
            wait_for_ssh(port)
            # This path belongs to the disposable container, not the host filesystem.
            public = docker("exec", container, "cat", "/tmp/ssh_host_ed25519_key.pub")
            records.append(
                {"name": name, "port": port, "public": public, "container": container},
            )
        known_hosts = directory / "known_hosts"
        known_hosts.write_text(
            "".join(f"[127.0.0.1]:{r['port']} {r['public']}\n" for r in records),
        )
        ssh_config = directory / "ssh_config"
        ssh_config.write_text(
            "".join(
                f"Host {r['name']}\n  HostName 127.0.0.1\n  Port {r['port']}\n"
                f"  User tunnel\n  IdentityFile {key}\n  IdentitiesOnly yes\n"
                f"  IdentityAgent none\n  UserKnownHostsFile {known_hosts}\n"
                "  GlobalKnownHostsFile /dev/null\n"
                for r in records
            ),
        )
        yield ssh_config, known_hosts, records
    finally:
        # Remove only resources owned by this fixture, including after failed setup.
        errors: list[Exception] = []
        for container in reversed(containers):
            try:
                docker("rm", "-f", container)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(exc)
        if image_ready:
            try:
                docker("image", "rm", "-f", image)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(exc)
        if errors:
            message = "SSH fixture cleanup failed"
            raise ExceptionGroup(message, errors)


def manager_for(
    ssh_servers: SSHServers,
    monkeypatch: pytest.MonkeyPatch,
) -> TunnelManager:
    """Build a production manager isolated from personal SSH config and agents."""
    ssh_config, _, _ = ssh_servers
    cfg = Config.model_validate(
        {
            "servers": [{"host": "one"}, {"host": "two"}],
            "proxy": {"ip": "127.0.0.1", "port": free_port()},
            "http_proxy": {"enabled": False},
            "state_file": None,
            "cooldown_seconds": 0,
            "startup_timeout_seconds": 5,
            "stop_timeout_seconds": 2,
        },
    )
    manager = TunnelManager(cfg)
    command = manager.command
    ssh = executable_for("ssh")
    # Run the production OpenSSH command, isolating only its config file from ~/.ssh.
    monkeypatch.setattr(
        manager,
        "command",
        lambda server: [ssh, "-F", str(ssh_config), *command(server)[1:]],
    )
    return manager


async def forwarded_body(manager: TunnelManager) -> bytes:
    """Fetch the fixture identity through the real OpenSSH SOCKS forwarder."""
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        manager.config.proxy.port,
    )
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00", (
            "OpenSSH rejected SOCKS authentication"
        )
        writer.write(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + (8080).to_bytes(2, "big"))
        await writer.drain()
        reply = await reader.readexactly(4)
        assert reply[:3] == b"\x05\x00\x00", "OpenSSH rejected SOCKS CONNECT"
        length = {1: 4, 4: 16}.get(reply[3])
        domain_address = 3
        if reply[3] == domain_address:
            length = (await reader.readexactly(1))[0]
        if length is None:
            pytest.fail(f"Unexpected SOCKS address type: {reply[3]}")
        await reader.readexactly(length + 2)
        writer.write(b"GET / HTTP/1.0\r\nHost: fixture\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert response.startswith(b"HTTP/1.0 200"), "Forwarded HTTP request failed"
        return response.split(b"\r\n\r\n", 1)[1].strip()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_real_ssh_rotation_forwarding_and_cleanup(
    ssh_servers: SSHServers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotate real forwards and release both processes and listening sockets."""
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        first = await manager.rotate()
        old = manager.process
        if old is None:
            pytest.fail("First rotation did not start an SSH process")
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"one", (
            "First forward reached the wrong server"
        )
        second = await manager.rotate()
        assert old.returncode is not None, "Rotation left the old SSH process running"
        assert first["connection_id"] != second["connection_id"], (
            "Rotation reused the connection identity"
        )
        assert second["started_at"] >= first["started_at"], (
            "Rotation moved the start time backwards"
        )
        assert second["server"] == "two", "Rotation did not select the second server"
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"two", (
            "Second forward reached the wrong server"
        )
        current = manager.process
        if current is None:
            pytest.fail("Second rotation did not start an SSH process")
    finally:
        await manager.close()
    assert current.returncode is not None, "Close left the current SSH process running"
    assert manager.status()["state"] == "disconnected", (
        "Closed manager remained connected"
    )
    # Cleanup releases the actual SOCKS listening socket.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", manager.config.proxy.port))


async def test_real_ssh_falls_through_to_the_trusted_server(
    ssh_servers: SSHServers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an untrusted host and forward only through the next trusted server."""
    _, known_hosts, records = ssh_servers
    original = known_hosts.read_text()
    # Trust only server two: server one's key must be rejected, never accepted.
    known_hosts.write_text(f"[127.0.0.1]:{records[1]['port']} {records[1]['public']}\n")
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        # One rotation: ssh for server one dies on the host key check and the
        # rotation moves straight on to server two without waiting for a trigger.
        status = await manager.rotate()
        assert status["server"] == "two", "Untrusted server was not skipped"
        assert status["state"] == "connected", "Trusted fallback did not connect"
        assert manager.last_error is None, "Successful fallback retained an error"
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"two", (
            "Fallback reached an untrusted server"
        )
    finally:
        await manager.close()
        known_hosts.write_text(original)


async def test_real_ssh_reports_failure_when_every_host_is_rejected(
    ssh_servers: SSHServers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose total trust failure and recover after disposable host keys are trusted."""
    _, known_hosts, _records = ssh_servers
    original = known_hosts.read_text()
    known_hosts.write_text("")
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        with pytest.raises(TunnelFailure, match="SSH exited"):
            await manager.rotate()
        assert manager.process is None, "Rejected hosts left an SSH process adopted"
        assert manager.status()["connection_id"] is None, (
            "Rejected hosts retained connection identity"
        )
        error = manager.last_error
        if error is None:
            pytest.fail("Rejected hosts did not record an SSH failure")
        assert "SSH exited with code" in error, (
            "Rejected hosts recorded the wrong failure"
        )
        # Trusting the servers again lets the next rotation recover.
        known_hosts.write_text(original)
        assert (await manager.rotate())["server"] == "one", (
            "Restored trust did not recover the first server"
        )
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"one", (
            "Recovered forward reached the wrong server"
        )
    finally:
        await manager.close()


async def test_real_ssh_server_loss_and_recovery(
    ssh_servers: SSHServers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clear stale state after server loss and recover on the remaining real server."""
    records = ssh_servers[2]
    manager = manager_for(ssh_servers, monkeypatch)
    try:
        await manager.rotate()
        process = manager.process
        if process is None:
            pytest.fail("Rotation did not start an SSH process")
        await asyncio.to_thread(docker, "stop", "--time", "1", records[0]["container"])
        await asyncio.wait_for(process.wait(), 10)
        status = manager.status()
        assert status["state"] == "disconnected", "Server loss retained connected state"
        assert status["connection_id"] is None, (
            "Server loss retained connection identity"
        )
        assert status["started_at"] is None, "Server loss retained start time"
        assert (await manager.rotate())["server"] == "two", (
            "Recovery did not select the surviving server"
        )
        assert await asyncio.wait_for(forwarded_body(manager), 5) == b"two", (
            "Recovery reached the wrong server"
        )
    finally:
        await manager.close()
