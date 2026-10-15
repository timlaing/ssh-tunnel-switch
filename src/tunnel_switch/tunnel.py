import asyncio
from datetime import datetime, timezone
import socket
import time
from uuid import uuid4
from .config import Config, Server
from .state import load_last_server, save_last_server


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
        self.connection_id = None
        self.started_at = None
        self.server_name = None
        self.last_error = None
        remembered = next((index for index, server in enumerate(config.servers)
                           if server.name == config.server_name), None)
        if remembered is None and config.state_file is not None:
            remembered = load_last_server(config.state_file, config.servers)
        # rotate() advances before connecting: the first connection after restart
        # resumes the saved server, then subsequent rotations advance normally.
        self._index = remembered - 1 if remembered is not None else -1
        self._busy = False
        self._last_rotation = None

    def status(self) -> dict:
        alive = self.process is not None and self.process.returncode is None
        return {
            "state": "rotating" if self._busy else "connected" if alive else "disconnected",
            "connection_id": self.connection_id if alive else None,
            "started_at": self.started_at.isoformat() if alive and self.started_at else None,
            "server": self.server_name if alive else None,
            "proxy": self.config.proxy.model_dump(mode="json"),
            "last_error": self.last_error,
        }

    def command(self, server: Server) -> list[str]:
        ip = str(self.config.proxy.ip)
        bind = f"[{ip}]" if ":" in ip else ip
        args = ["ssh", "-N", "-T", "-D", f"{bind}:{self.config.proxy.port}",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-o", f"ConnectTimeout={int(self.config.startup_timeout_seconds) or 1}"]
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
        self.connection_id = self.started_at = self.server_name = None

    async def _ready(self):
        ip = str(self.config.proxy.ip)
        if ip == "0.0.0.0":
            ip = "127.0.0.1"
        elif ip == "::":
            ip = "::1"
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, self.config.proxy.port), 0.5)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            return await asyncio.wait_for(reader.readexactly(2), 0.5) == b"\x05\x00"
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return False
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()

    def _ensure_socket_free(self):
        family = socket.AF_INET6 if self.config.proxy.ip.version == 6 else socket.AF_INET
        with socket.socket(family) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((str(self.config.proxy.ip), self.config.proxy.port))
            except OSError as exc:
                raise TunnelFailure("SOCKS bind address is already occupied or unavailable") from exc

    async def rotate(self) -> dict:
        # No await before admission: atomic within the single service event loop.
        if self._busy:
            raise RotationRejected("Tunnel rotation already in progress")
        now = time.monotonic()
        if self._last_rotation is not None:
            remaining = self.config.cooldown_seconds - (now - self._last_rotation)
            if remaining > 0:
                raise RotationRejected("Tunnel rotation cooldown active", int(remaining) + 1)
        self._busy = True
        self.last_error = None
        try:
            await self._stop()
            self._ensure_socket_free()
            self._index = (self._index + 1) % len(self.config.servers)
            server = self.config.servers[self._index]
            self.process = await asyncio.create_subprocess_exec(
                *self.command(server), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            deadline = time.monotonic() + self.config.startup_timeout_seconds
            while time.monotonic() < deadline:
                if self.process.returncode is not None:
                    raise TunnelFailure(f"SSH exited with code {self.process.returncode}; check authentication, known_hosts and forwarding permissions")
                if await self._ready():
                    await asyncio.sleep(0.05)
                    if self.process.returncode is None:
                        self.connection_id = str(uuid4())
                        self.started_at = datetime.now(timezone.utc)
                        self.server_name = server.name
                        if self.config.state_file is not None:
                            # Serialize completion even on cancellation: an older
                            # background save must never overwrite a newer rotation.
                            save = asyncio.create_task(asyncio.to_thread(
                                save_last_server, self.config.state_file, server))
                            try:
                                await asyncio.shield(save)
                            except asyncio.CancelledError:
                                await save
                                raise
                        break
                await asyncio.sleep(0.1)
            else:
                raise TunnelFailure("SSH SOCKS startup timed out")
        except asyncio.CancelledError:
            await self._stop()
            raise
        except Exception as exc:
            self.last_error = str(exc)
            await self._stop()
            raise TunnelFailure(str(exc)) from exc
        finally:
            self._last_rotation = time.monotonic()
            self._busy = False
        return self.status()

    async def close(self):
        await self._stop()
