import asyncio
from datetime import datetime, timezone
import logging
import socket
import time
from uuid import uuid4
from .config import Config, Server
from .state import load_last_server, save_last_server

logger = logging.getLogger(__name__)


class RotationRejected(Exception):
    def __init__(self, message: str, retry_after: int = 1):
        super().__init__(message)
        self.retry_after = retry_after


class TunnelFailure(Exception):
    pass


class TunnelManager:
    def __init__(self, config: Config):
        self.config = config
        self.process = None
        # (connection_id, started_at, server_name) published as one immutable
        # snapshot: the status display thread reads it while rotate()/_stop()
        # write, so a torn read would mix values from different lifecycles.
        self._connection = None
        self.last_error = None
        remembered = next(
            (
                index
                for index, server in enumerate(config.servers)
                if server.name == config.server_name
            ),
            None,
        )
        if remembered is None and config.state_file is not None:
            remembered = load_last_server(config.state_file, config.servers)
        # rotate() advances before connecting: the first connection after restart
        # resumes the saved server, then subsequent rotations advance normally.
        self._index = remembered - 1 if remembered is not None else -1
        self._busy = False
        self._last_rotation = None

    def status(self) -> dict:
        process = self.process
        alive = process is not None and process.returncode is None
        state = "connected" if alive else "disconnected"
        if self._busy:
            state = "rotating"
        connection_id, started_at, server_name = self._connection or (None, None, None)
        if not alive:
            connection_id = started_at = server_name = None
        return {
            "state": state,
            "connection_id": connection_id,
            "started_at": started_at.isoformat() if started_at else None,
            "server": server_name,
            "proxy": self.config.proxy.model_dump(mode="json"),
            "last_error": self.last_error,
        }

    def command(self, server: Server) -> list[str]:
        ip = str(self.config.proxy.ip)
        bind = f"[{ip}]" if ":" in ip else ip
        args = [
            "ssh",
            "-N",
            "-T",
            "-D",
            f"{bind}:{self.config.proxy.port}",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            f"ConnectTimeout={int(self.config.startup_timeout_seconds) or 1}",
        ]
        if server.port is not None:
            args.extend(["-p", str(server.port)])
        if server.user is not None:
            args.extend(["-l", server.user])
        if server.identity_file:
            args.extend(["-i", str(server.identity_file)])
        return [*args, "--", server.host]

    async def _stop(self):
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.config.stop_timeout_seconds)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        self.process = None
        self._connection = None

    async def _ready(self):
        ip = str(self.config.proxy.ip)
        if ip == "0.0.0.0":
            ip = "127.0.0.1"
        elif ip == "::":
            ip = "::1"
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, self.config.proxy.port), 0.5
            )
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            return await asyncio.wait_for(reader.readexactly(2), 0.5) == b"\x05\x00"
        except (OSError, asyncio.IncompleteReadError):
            return False
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()

    def _ensure_socket_free(self):
        family = (
            socket.AF_INET6 if self.config.proxy.ip.version == 6 else socket.AF_INET
        )
        with socket.socket(family) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((str(self.config.proxy.ip), self.config.proxy.port))
            except OSError as exc:
                raise TunnelFailure(
                    "SOCKS bind address is already occupied or unavailable"
                ) from exc

    async def rotate(self) -> dict:
        # No await before admission: atomic within the single service event loop.
        if self._busy:
            raise RotationRejected("Tunnel rotation already in progress")
        now = time.monotonic()
        if self._last_rotation is not None:
            remaining = self.config.cooldown_seconds - (now - self._last_rotation)
            if remaining > 0:
                raise RotationRejected(
                    "Tunnel rotation cooldown active", int(remaining) + 1
                )
        self._busy = True
        self.last_error = None
        try:
            await self._rotate_across_servers()
        finally:
            self._last_rotation = time.monotonic()
            self._busy = False
        return self.status()

    async def _rotate_across_servers(self) -> None:
        """Connect through the next server and, if it fails, the rest of the list.

        Each server is tried at most once and without any cooldown between
        attempts, so an SSH connection failure falls through to the next server
        immediately. A rotation either stops at the first server that accepts
        SSH or, when every server is rejected, reports the last failure once.
        """
        servers = self.config.servers
        failures = []
        try:
            for attempt in range(len(servers)):
                # Select the target server first so every failure names it in the log.
                self._index = (self._index + 1) % len(servers)
                server = servers[self._index]
                try:
                    await self._connect(server)
                except Exception as exc:
                    failures.append((server.name, exc))
                    if attempt + 1 < len(servers):
                        logger.exception(
                            "Tunnel rotation to server %s failed; trying the next server",
                            server.name,
                        )
                    else:
                        logger.exception(
                            "Tunnel rotation to server %s failed", server.name
                        )
                    await self._stop()
                    continue
                self._connection = (
                    str(uuid4()),
                    datetime.now(timezone.utc),
                    server.name,
                )
                # The tunnel is up: failing to persist the selection must not
                # move on to another server, since the same state file would fail.
                try:
                    await self._remember_server(server)
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.exception(
                        "Tunnel connected to SSH server %s but saving its state failed",
                        server.name,
                    )
                    await self._stop()
                    raise TunnelFailure(str(exc)) from exc
                logger.info(
                    "Tunnel connected to SSH server %s (%s)", server.name, server.host
                )
                return
            _, last = failures[-1]
            self.last_error = str(last)
            logger.error(
                "Tunnel rotation failed on every server (%d tried): %s",
                len(servers),
                "; ".join(f"{name}: {error}" for name, error in failures),
            )
            raise TunnelFailure(str(last))
        except asyncio.CancelledError:
            # Cancellation during connect or state save must not leave a tunnel.
            await self._stop()
            raise

    async def _connect(self, server: Server):
        """Start SSH for *server* and wait until it proxies, or raise."""
        await self._stop()
        self._ensure_socket_free()
        self.process = await asyncio.create_subprocess_exec(
            *self.command(server),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._wait_until_ready()

    async def _wait_until_ready(self):
        """Wait for SOCKS readiness and a stable SSH process within its deadline.

        An SSH connection failure (auth, host key, unreachable host) makes ssh
        exit, which is reported as soon as it is seen rather than as a timeout:
        rotate() turns the raised TunnelFailure into a failover to the next
        server.
        """
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            self._raise_if_ssh_exited()
            if await self._ready():
                await asyncio.sleep(0.05)
                if self.process.returncode is None:
                    return
            await asyncio.sleep(0.1)
        # ssh may have died just as the deadline passed; report the real cause.
        self._raise_if_ssh_exited()
        raise TunnelFailure("SSH SOCKS startup timed out")

    def _raise_if_ssh_exited(self):
        if self.process.returncode is not None:
            raise TunnelFailure(
                f"SSH exited with code {self.process.returncode}; check authentication, known_hosts and forwarding permissions"
            )

    async def _remember_server(self, server):
        """Finish atomic persistence before allowing any subsequent rotation."""
        if self.config.state_file is None:
            return
        save = asyncio.create_task(
            asyncio.to_thread(save_last_server, self.config.state_file, server)
        )
        try:
            await asyncio.shield(save)
        except asyncio.CancelledError:
            await save
            raise

    async def close(self):
        await self._stop()
