"""Manage SSH tunnel lifecycles, server selection, and automatic recovery."""

import asyncio
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial
from ipaddress import IPv6Address
from threading import Lock
from uuid import uuid4

from .config import Config, Server
from .counters import RequestCounter
from .state import load_last_server, save_last_server

logger = logging.getLogger(__name__)


class RotationRejectedError(Exception):
    """Reject a concurrent or cooldown-limited tunnel change."""

    def __init__(self, message: str, retry_after: int = 1) -> None:
        """Store the admission failure and suggested retry interval."""
        super().__init__(message)
        self.retry_after = retry_after


class TunnelFailureError(Exception):
    """Report an SSH connection, readiness, or state persistence failure."""


# Keep the established exception imports and catch sites compatible.
RotationRejected = RotationRejectedError
TunnelFailure = TunnelFailureError


def _running(process: asyncio.subprocess.Process | None) -> bool:
    """Whether an SSH process was started and has not exited yet."""
    return process is not None and process.returncode is None


class TunnelManager:
    """Own one SSH process and publish coherent connection status snapshots."""

    def __init__(self, config: Config, counter: RequestCounter | None = None) -> None:
        """Initialize an idle manager with the configured or remembered selection."""
        self.config = config
        # Shared with the HTTP proxy, which records into it while status()
        # reports it: the request totals belong to the connection lifecycle
        # this class owns, so they are read from here.
        self.counter = counter or RequestCounter()
        self.process: asyncio.subprocess.Process | None = None
        # (connection_id, started_at, server_name) published as one immutable
        # snapshot: the status display thread reads it while rotate()/_stop()
        # write, so a torn read would mix values from different lifecycles.
        self._connection: tuple[str, datetime, str | None] | None = None
        # Guards everything status() reports against the writes that publish a
        # connection, so one status() call describes one instant instead of
        # straddling a rotation: the display thread and the service loop share
        # it, and it is never held across an await.
        self._state_lock = Lock()
        self.last_error: str | None = None
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
        self._last_rotation: float | None = None
        # False until the first connection attempt: before that, "the current
        # server" is the one a rotation would pick, not one we have tried.
        self._attempted = False
        # Desired state is separate from process liveness: an exited SSH should
        # recover, but an intentional disconnect (or idle launch) should not.
        self._want_connection = False
        # Manual changes invalidate any retry deadline/backoff the monitor held.
        self._generation = 0
        self._reconnect_server: tuple[str | None, str] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._reported_exit: asyncio.subprocess.Process | None = None

    @property
    def busy(self) -> bool:
        """Return whether a tunnel change currently holds the admission gate."""
        return self._busy

    def adopted(self) -> bool:
        """Whether the connection this manager adopted is the one running.

        The HTTP proxy asks before recording a request: while no adopted
        tunnel serves the SOCKS port, whatever answers on it belongs to some
        other listener, and its requests are not this tunnel's to count.
        """
        return self.connection_id() is not None

    def connection_id(self) -> str | None:
        """Return the live adopted generation, refusing admission during changes."""
        with self._state_lock:
            if self._busy or not _running(self.process) or self._connection is None:
                return None
            return self._connection[0]

    def _admit(self, *, cooldown: bool = True) -> None:
        """Refuse a connection change while one runs or the cooldown holds.

        ``cooldown=False`` keeps the busy gate but skips the backoff: an
        interactive key is a deliberate request from the person watching the
        panel, so it must not be refused for having just rotated.
        """
        if self._busy:
            message = "Tunnel rotation already in progress"
            raise RotationRejected(message)
        if not cooldown:
            return
        now = time.monotonic()
        if self._last_rotation is not None:
            remaining = self.config.cooldown_seconds - (now - self._last_rotation)
            if remaining > 0:
                message = "Tunnel rotation cooldown active"
                raise RotationRejected(message, int(remaining) + 1)

    def status(self) -> dict[str, object]:
        """Snapshot connection identity, liveness, request totals, and errors."""
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
        """Build noninteractive SSH arguments with strict host-key verification."""
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

    async def _stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self.config.stop_timeout_seconds)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        with self._state_lock:
            self.process = None
            self._connection = None

    async def _ready(self) -> bool:
        ip = str(self.config.proxy.ip)
        if self.config.proxy.ip.is_unspecified:
            ip = "::1" if isinstance(self.config.proxy.ip, IPv6Address) else "127.0.0.1"
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, self.config.proxy.port),
                0.5,
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

    def _ensure_socket_free(self) -> None:
        family = (
            socket.AF_INET6
            if isinstance(self.config.proxy.ip, IPv6Address)
            else socket.AF_INET
        )
        with socket.socket(family) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((str(self.config.proxy.ip), self.config.proxy.port))
            except OSError as exc:
                message = "SOCKS bind address is already occupied or unavailable"
                raise TunnelFailure(message) from exc

    async def rotate(
        self,
        pre_connect: Callable[[], None] | None = None,
        *,
        ignore_cooldown: bool = False,
        reason: str | None = None,
    ) -> dict[str, object]:
        """Connect through the next server, falling back to the rest of the list.

        ``pre_connect`` runs once the rotation is admitted and before the
        first await, so admission and the deferred SOCKS bind adoption it
        unlocks are one atomic step in the service event loop.
        ``ignore_cooldown`` skips the cooldown but keeps the busy gate, which
        is what the interactive rotate key uses.
        ``reason`` is an optional string explaining why the rotation was triggered
        (e.g., from a client detecting rate limits).
        """
        # No await before admission: atomic within the single service event loop.
        self._admit(cooldown=not ignore_cooldown)
        if reason:
            # Sanitize: trim, collapse whitespace, truncate to 500 chars.
            msg = re.sub(r"\s+", " ", reason.strip())[:500]
            logger.info("Tunnel rotation requested: %s", msg)
        return await self._change(self._rotate_across_servers, pre_connect)

    async def select_server(
        self,
        name: str,
        pre_connect: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Connect through one named server, without trying the others.

        An explicit choice skips the cooldown for the same reason as
        ``ignore_cooldown`` on rotate(): the operator picked that server, so
        it either comes up or its failure is reported. This immediate selection
        never fails over. If automatic recovery is enabled, subsequent recovery
        attempts may fail over after retrying the selected server.
        """
        servers = self.config.servers
        server = next((s for s in servers if s.name == name), None)
        if server is None:
            message = (
                f"Unknown server: {name}. Choose one of: "
                f"{', '.join(s.name for s in servers)}"
            )
            raise ValueError(message)
        self._admit(cooldown=False)
        return await self._change(
            lambda: self._connect_selected(servers.index(server)),
            pre_connect,
        )

    async def restart(
        self,
        pre_connect: Callable[[], None] | None = None,
    ) -> dict[str, object]:
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
            lambda: self._rotate_across_servers(first_index=first),
            pre_connect,
        )

    async def disconnect(self) -> dict[str, object]:
        """Stop the tunnel without connecting elsewhere.

        Stopping never contacts an SSH server, so it also clears the cooldown
        gate: a subsequent restart() or rotate() reconnects immediately. The
        busy flag is held across the stop so no rotation or restart can be
        admitted while the old connection is still shutting down.
        """
        if self._busy:
            message = "Tunnel rotation already in progress"
            raise RotationRejected(message)
        self._busy = True
        self._want_connection = False
        self._generation += 1
        try:
            await self._stop()
        finally:
            self._last_rotation = None
            self._busy = False
        logger.info("Tunnel disconnected")
        return self.status()

    async def _change(
        self,
        connect: Callable[[], Awaitable[None]],
        pre_connect: Callable[[], None] | None = None,
        *,
        recovery: bool = False,
    ) -> dict[str, object]:
        self._busy = True
        if not recovery:
            self._want_connection = True
            self._generation += 1
            self._reconnect_server = None
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
                            "Tunnel rotation to server %s failed; "
                            "trying the next server",
                            server.name,
                        )
                    else:
                        logger.exception(
                            "Tunnel rotation to server %s failed",
                            server.name,
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
                datetime.now(UTC),
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
        self._reconnect_server = (server.name, server.host)
        self._reported_exit = None

    async def _connect(self, server: Server) -> None:
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

    async def _wait_until_ready(self) -> None:
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
        message = "SSH SOCKS startup timed out"
        raise TunnelFailure(message)

    def _raise_if_ssh_exited(self) -> None:
        if self.process.returncode is not None:
            message = (
                f"SSH exited with code {self.process.returncode}; "
                "check authentication, known_hosts and forwarding permissions"
            )
            raise TunnelFailure(message)

    async def _remember_server(self, server: Server) -> None:
        """Finish atomic persistence before allowing any subsequent rotation."""
        if self.config.state_file is None:
            return
        save = asyncio.create_task(
            asyncio.to_thread(save_last_server, self.config.state_file, server),
        )
        try:
            await asyncio.shield(save)
        except asyncio.CancelledError:
            await save
            raise

    def start_monitor(
        self,
        pre_connect: Callable[[], None] | None = None,
        *,
        interval: float = 1.0,
    ) -> None:
        """Watch SSH exits until close(), including while auto recovery is disabled."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(
                self._monitor(pre_connect, interval),
            )

    async def _monitor(
        self,
        pre_connect: Callable[[], None] | None,
        interval: float,
    ) -> None:
        generation = None
        retry_at = None
        attempts = 0
        delay = self.config.reconnect_delay_seconds
        while True:
            await asyncio.sleep(interval)
            if generation != self._generation:
                generation = self._generation
                retry_at = None
                attempts = 0
                delay = self.config.reconnect_delay_seconds
            if self._busy:
                continue
            self._observe_ssh_exit()
            if not self._needs_recovery():
                retry_at = None
                attempts = 0
                delay = self.config.reconnect_delay_seconds
                continue
            if attempts >= self.config.reconnect_attempts:
                continue
            if retry_at is None:
                logger.warning("Tunnel disconnected; reconnecting immediately")
                delay = self.config.reconnect_delay_seconds
            elif time.monotonic() < retry_at:
                continue
            self._admit(cooldown=False)
            attempts += 1
            try:
                await self._recover(pre_connect)
            except Exception as exc:
                logger.debug("Tunnel recovery attempt failed", exc_info=True)
                self.last_error = str(exc)
                delay = min(delay, self.config.reconnect_max_delay_seconds)
                retry_at = time.monotonic() + delay
                self._report_recovery_failure(attempts, delay, exc)
                delay = min(delay * 2, self.config.reconnect_max_delay_seconds)
            else:
                retry_at = None
                attempts = 0
                delay = self.config.reconnect_delay_seconds

    def _needs_recovery(self) -> bool:
        return (
            self._want_connection
            and self.config.reconnect_attempts > 0
            and not self.adopted()
        )

    def _observe_ssh_exit(self) -> None:
        """Report each adopted process exit once, even with recovery disabled."""
        process = self.process
        if (
            process is None
            or process.returncode is None
            or self._connection is None
            or process is self._reported_exit
        ):
            return
        self._reported_exit = process
        self.last_error = f"SSH tunnel exited with code {process.returncode}"
        logger.warning("%s", self.last_error)

    async def _recover(self, pre_connect: Callable[[], None] | None) -> None:
        # Resolve by identity, not the old list index: a reload may have
        # reordered or removed the previously connected server.
        first = next(
            (
                i
                for i, server in enumerate(self.config.servers)
                if (server.name, server.host) == self._reconnect_server
            ),
            self._index % len(self.config.servers),
        )
        await self._change(
            partial(self._rotate_across_servers, first_index=first),
            pre_connect,
            recovery=True,
        )

    def _report_recovery_failure(
        self,
        attempts: int,
        delay: float,
        exc: Exception,
    ) -> None:
        if attempts >= self.config.reconnect_attempts:
            logger.warning(
                "Tunnel reconnection failed; exhausted %d recovery attempts: %s",
                attempts,
                exc,
            )
        else:
            logger.warning(
                "Tunnel reconnection failed; retrying in %.1f seconds: %s",
                delay,
                exc,
            )

    async def close(self) -> None:
        """Cancel exit monitoring and stop SSH, preserving caller cancellation."""
        self._want_connection = False
        self._generation += 1
        task, self._monitor_task = self._monitor_task, None
        try:
            if task is not None:
                task.cancel()
                # Collect the child's expected cancellation, but cancellation
                # of close() itself must propagate after the final SSH cleanup.
                (outcome,) = await asyncio.gather(task, return_exceptions=True)
                if isinstance(outcome, Exception):
                    raise outcome
        finally:
            await self._stop()
