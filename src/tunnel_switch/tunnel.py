import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import logging
import socket
import time
from threading import Lock
from uuid import uuid4
from .config import Config, Server
from .counters import RequestCounter
from .state import load_last_server, save_last_server

logger = logging.getLogger(__name__)


class RotationRejected(Exception):
    def __init__(self, message: str, retry_after: int = 1):
        super().__init__(message)
        self.retry_after = retry_after


class TunnelFailure(Exception):
    pass


def _running(process) -> bool:
    """Whether an SSH process was started and has not exited yet."""
    return process is not None and process.returncode is None


class TunnelManager:
    def __init__(self, config: Config, counter: RequestCounter | None = None):
        self.config = config
        # Shared with the HTTP proxy, which records into it while status()
        # reports it: the request totals belong to the connection lifecycle
        # this class owns, so they are read from here.
        self.counter = counter or RequestCounter()
        self.process = None
        # (connection_id, started_at, server_name) published as one immutable
        # snapshot: the status display thread reads it while rotate()/_stop()
        # write, so a torn read would mix values from different lifecycles.
        self._connection = None
        # Guards everything status() reports against the writes that publish a
        # connection, so one status() call describes one instant instead of
        # straddling a rotation: the display thread and the service loop share
        # it, and it is never held across an await.
        self._state_lock = Lock()
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
        # False until the first connection attempt: before that, "the current
        # server" is the one a rotation would pick, not one we have tried.
        self._attempted = False

    @property
    def busy(self) -> bool:
        return self._busy

    def adopted(self) -> bool:
        """Whether the connection this manager adopted is the one running.

        The HTTP proxy asks before recording a request: while no adopted
        tunnel serves the SOCKS port, whatever answers on it belongs to some
        other listener, and its requests are not this tunnel's to count.
        """
        with self._state_lock:
            return _running(self.process) and self._connection is not None

    def _admit(self, cooldown: bool = True) -> None:
        """Refuse a connection change while one runs or the cooldown holds.

        ``cooldown=False`` keeps the busy gate but skips the backoff: an
        interactive key is a deliberate request from the person watching the
        panel, so it must not be refused for having just rotated.
        """
        if self._busy:
            raise RotationRejected("Tunnel rotation already in progress")
        if not cooldown:
            return
        now = time.monotonic()
        if self._last_rotation is not None:
            remaining = self.config.cooldown_seconds - (now - self._last_rotation)
            if remaining > 0:
                raise RotationRejected(
                    "Tunnel rotation cooldown active", int(remaining) + 1
                )

    def status(self) -> dict:
        # One lock-guarded read: the liveness check, the connection snapshot
        # and the counter snapshot describe the same instant, so ssh exiting
        # or a rotation publishing a new connection cannot leave the totals
        # labelled by a different connection than the fields beside them.
        with self._state_lock:
            process = self.process
            alive = _running(process)
            connection = self._connection
            requests = self.counter.snapshot(active=alive and connection is not None)
        state = "connected" if alive else "disconnected"
        if self._busy:
            state = "rotating"
        connection_id, started_at, server_name = connection or (None, None, None)
        if not alive:
            connection_id = started_at = server_name = None
        return {
            "state": state,
            "connection_id": connection_id,
            "started_at": started_at.isoformat() if started_at else None,
            "server": server_name,
            "proxy": self.config.proxy.model_dump(mode="json"),
            "last_error": self.last_error,
            # Only the adopted connection that is still running may own the
            # current request total; a rotation in between or a tunnel that
            # exited on its own reports its counts as the last total instead.
            "requests": requests,
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
        with self._state_lock:
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

    async def rotate(
        self,
        pre_connect: Callable[[], None] | None = None,
        *,
        ignore_cooldown: bool = False,
    ) -> dict:
        """Connect through the next server, falling back to the rest of the list.

        ``pre_connect`` runs once the rotation is admitted and before the
        first await, so admission and the deferred SOCKS bind adoption it
        unlocks are one atomic step in the service event loop.
        ``ignore_cooldown`` skips the cooldown but keeps the busy gate, which
        is what the interactive rotate key uses.
        """
        # No await before admission: atomic within the single service event loop.
        self._admit(cooldown=not ignore_cooldown)
        return await self._change(self._rotate_across_servers, pre_connect)

    async def select_server(
        self,
        name: str,
        pre_connect: Callable[[], None] | None = None,
    ) -> dict:
        """Connect through one named server, without trying the others.

        An explicit choice skips the cooldown for the same reason as
        ``ignore_cooldown`` on rotate(): the operator picked that server, so
        it either comes up or its failure is reported. No failover runs —
        connecting elsewhere would not be the selection that was asked for.
        """
        servers = self.config.servers
        server = next((s for s in servers if s.name == name), None)
        if server is None:
            raise ValueError(
                f"Unknown server: {name}. Choose one of: "
                f"{', '.join(s.name for s in servers)}"
            )
        self._admit(cooldown=False)
        return await self._change(
            lambda: self._connect_selected(servers.index(server)), pre_connect
        )

    async def restart(self, pre_connect: Callable[[], None] | None = None) -> dict:
        """Reconnect to the server the rotation index currently names.

        The index is pinned instead of advanced, so a restart bounces the
        current server; if it refuses SSH, the same failover as rotate() runs.
        Before the first attempt there is no current server to bounce, so the
        restart picks the server the next rotation would have (the saved one,
        after a service restart).
        """
        self._admit()
        first = self._index if self._attempted else None
        return await self._change(
            lambda: self._rotate_across_servers(first_index=first), pre_connect
        )

    async def disconnect(self) -> dict:
        """Stop the tunnel without connecting elsewhere.

        Stopping never contacts an SSH server, so it also clears the cooldown
        gate: a subsequent restart() or rotate() reconnects immediately. The
        busy flag is held across the stop so no rotation or restart can be
        admitted while the old connection is still shutting down.
        """
        if self._busy:
            raise RotationRejected("Tunnel rotation already in progress")
        self._busy = True
        try:
            await self._stop()
        finally:
            self._last_rotation = None
            self._busy = False
        logger.info("Tunnel disconnected")
        return self.status()

    async def _change(
        self, connect, pre_connect: Callable[[], None] | None = None
    ) -> dict:
        self._busy = True
        self.last_error = None
        try:
            if pre_connect is not None:
                # Synchronous and await-free: admitted state and the adopted
                # bind become visible to the rest of the loop at the same
                # instant, and the finally below still releases the busy flag.
                pre_connect()
            await connect()
        finally:
            self._last_rotation = time.monotonic()
            self._busy = False
        return self.status()

    async def _rotate_across_servers(self, first_index: int | None = None) -> None:
        """Connect through the next server and, if it fails, the rest of the list.

        Each server is tried at most once and without any cooldown between
        attempts, so an SSH connection failure falls through to the next server
        immediately. A rotation either stops at the first server that accepts
        SSH or, when every server is rejected, reports the last failure once.
        ``first_index`` pins the first server tried; by default the rotation
        advances from the current one.
        """
        servers = self.config.servers
        if first_index is None:
            first_index = (self._index + 1) % len(servers)
        self._index = (first_index - 1) % len(servers)
        # From here on a restart pins this index instead of advancing one.
        self._attempted = True
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
                await self._adopt(server)
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

    async def _connect_selected(self, index: int) -> None:
        """Connect to the server at ``index`` alone: no advance, no failover.

        The index is pinned first so a restart() after this selection bounces
        the same server instead of moving on.
        """
        server = self.config.servers[index]
        self._index = index
        # From here on a restart pins this index instead of advancing one.
        self._attempted = True
        try:
            await self._connect(server)
        except asyncio.CancelledError:
            await self._stop()
            raise
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Tunnel selection of server %s failed", server.name)
            await self._stop()
            raise TunnelFailure(str(exc)) from exc
        await self._adopt(server)

    async def _adopt(self, server: Server) -> None:
        """Publish the new connection and persist the selection."""
        # Published and the totals reopened as one step: a status() reading
        # between the two would pair the new connection with the old counts.
        with self._state_lock:
            self._connection = (
                str(uuid4()),
                datetime.now(timezone.utc),
                server.name,
            )
            # The new connection owns the request totals from here on: the
            # counts the previous one collected become the last value shown.
            self.counter.begin()
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
        logger.info("Tunnel connected to SSH server %s (%s)", server.name, server.host)

    async def _connect(self, server: Server):
        """Start SSH for *server* and wait until it proxies, or raise."""
        await self._stop()
        self._ensure_socket_free()
        process = await asyncio.create_subprocess_exec(
            *self.command(server),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with self._state_lock:
            self.process = process
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
