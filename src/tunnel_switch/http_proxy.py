"""HTTP/1.1 forward proxy and CONNECT bridge over the managed SSH SOCKS5 socket."""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from http import HTTPStatus
from ipaddress import IPv4Address, ip_address
from urllib.parse import urlsplit

import h11

from .config import Config
from .counters import RequestCounter

HTTP_PORT = 80
MAX_PORT = 65535
MAX_HOSTNAME_BYTES = 255
READ_BYTES = 65536
SOCKS_IPV4 = 1
SOCKS_DOMAIN = 3
SOCKS_IPV6 = 4

type StreamPair = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class ProxyError(Exception):
    """Report a SOCKS negotiation or HTTP forwarding protocol failure."""


def destination(target: str, connect: bool = False) -> tuple[str, int, str]:
    """Validate a proxy target and return its host, port and origin-form path."""
    url = urlsplit(f"//{target}" if connect else target)
    if url.username is not None or url.password is not None or url.fragment:
        message = "Invalid proxy target"
        raise ValueError(message)
    if not url.hostname or (not connect and url.scheme != "http"):
        message = "Use an absolute HTTP URL or CONNECT for HTTPS"
        raise ValueError(message)
    if connect and (url.path or url.query or url.port is None):
        message = "CONNECT requires host:port"
        raise ValueError(message)
    port = url.port if url.port is not None else HTTP_PORT
    if not 1 <= port <= MAX_PORT:
        message = "Invalid destination port"
        raise ValueError(message)
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    return url.hostname, port, path


def headers_for_forwarding(
    headers: Sequence[tuple[bytes, bytes]],
    *,
    host: str | None = None,
) -> list[tuple[bytes, bytes]]:
    """Validate framing, strip hop-by-hop headers and optionally replace Host."""
    names = {name.lower() for name, _ in headers}
    if {b"content-length", b"transfer-encoding"} <= names:
        message = "Ambiguous HTTP body framing"
        raise ValueError(message)
    connection_tokens = {
        item.strip().lower()
        for name, value in headers
        if name.lower() == b"connection"
        for item in value.split(b",")
    }
    if connection_tokens & {b"host", b"content-length", b"transfer-encoding"}:
        message = "Invalid Connection header"
        raise ValueError(message)
    stripped = connection_tokens | {
        b"connection",
        b"proxy-connection",
        b"proxy-authorization",
        b"proxy-authenticate",
        b"keep-alive",
        b"upgrade",
    }
    if host is not None:
        stripped.add(b"host")
    result = [(name, value) for name, value in headers if name.lower() not in stripped]
    if host is not None:
        result.append((b"host", host.encode("ascii")))
    result.append((b"connection", b"close"))
    return result


class HTTPProxyServer:
    """Bridge bounded HTTP connections through the managed tunnel's SOCKS port."""

    def __init__(
        self,
        config: Config,
        counter: RequestCounter | None = None,
        active: Callable[[], bool] | None = None,
        connection_id: Callable[[], str | None] | None = None,
    ) -> None:
        """Use the manager's configuration, request counter and ownership gate."""
        self.config = config
        # The tunnel manager's counter, so the requests recorded here are the
        # ones its status reports for the connection they were carried over.
        self.counter = counter or RequestCounter()
        # The manager's adopted-connection check: an unrelated SOCKS listener
        # must never carry traffic while the managed tunnel is unavailable.
        self.active = active
        self.connection_id = connection_id
        self.server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None] | None] = set()
        self._closing = False

    async def start(self) -> None:
        """Start the listener when HTTP proxying is enabled."""
        if self.config.http_proxy.enabled:
            self.server = await asyncio.start_server(
                self._accept,
                str(self.config.http_proxy.ip),
                self.config.http_proxy.port,
                limit=self.config.http_proxy.header_limit_bytes,
            )

    async def close(self) -> None:
        """Stop accepting clients and cancel and drain active bridge tasks."""
        self._closing = True
        if self.server:
            self.server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()

    async def _socks(
        self,
        host: str,
        port: int,
        connection_id: str | None = None,
    ) -> StreamPair:
        connection_id = self._require_active_tunnel(connection_id)
        proxy_address = self.config.proxy.ip
        proxy_ip = str(proxy_address)
        if proxy_address.is_unspecified:
            proxy_ip = "127.0.0.1" if isinstance(proxy_address, IPv4Address) else "::1"
        reader, writer = await asyncio.open_connection(proxy_ip, self.config.proxy.port)
        try:
            self._require_active_tunnel(connection_id)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            _validate_socks_auth(await reader.readexactly(2))
            encoded = _socks_address(host)
            self._require_active_tunnel(connection_id)
            writer.write(b"\x05\x01\x00" + encoded + port.to_bytes(2, "big"))
            await writer.drain()
            await _read_socks_reply(reader)
            self._require_active_tunnel(connection_id)
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
        else:
            return reader, writer

    def _require_active_tunnel(self, expected: str | None = None) -> str | None:
        """Require the same adopted generation throughout upstream negotiation."""
        if self.active is not None and not self.active():
            message = "Managed tunnel unavailable"
            raise ProxyError(message)
        if self.connection_id is None:
            return None
        current = self.connection_id()
        if current is None or (expected is not None and current != expected):
            message = "Managed tunnel changed or became unavailable"
            raise ProxyError(message)
        return current

    async def _read_event(
        self,
        connection: h11.Connection,
        reader: asyncio.StreamReader,
    ) -> h11.Event | type[h11.PAUSED]:
        while True:
            event = connection.next_event()
            if event is not h11.NEED_DATA:
                return event
            data = await asyncio.wait_for(
                reader.read(READ_BYTES),
                self.config.http_proxy.idle_timeout_seconds,
            )
            connection.receive_data(data)

    async def _relay_connect(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        loop = asyncio.get_running_loop()
        last_activity = loop.time()

        async def pump(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            nonlocal last_activity
            while data := await reader.read(READ_BYTES):
                last_activity = loop.time()
                writer.write(data)
                await writer.drain()
                last_activity = loop.time()
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()

        async def watch_idle() -> None:
            # HTTPS requests can be quiet in one direction throughout a streamed
            # response. Only silence across the entire tunnel is inactivity.
            while True:
                remaining = self.config.http_proxy.idle_timeout_seconds - (
                    loop.time() - last_activity
                )
                if remaining <= 0:
                    message = "CONNECT tunnel inactive"
                    raise TimeoutError(message)
                await asyncio.sleep(remaining)

        tasks = [
            asyncio.create_task(pump(client_reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, client_writer)),
            asyncio.create_task(watch_idle()),
        ]
        try:
            done = await _first_completed(tasks)
            if tasks[1] not in done:
                # Preserve client half-close: the upstream may still send its
                # response, but the shared inactivity deadline still applies.
                await _first_completed(tasks[1:])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_body(
        self,
        inbound: h11.Connection,
        reader: asyncio.StreamReader,
        outbound: h11.Connection,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            event = await self._read_event(inbound, reader)
            if not isinstance(event, (h11.Data, h11.EndOfMessage)):
                message = "Incomplete HTTP request body"
                raise ProxyError(message)
            upstream_writer.write(outbound.send(event))
            await upstream_writer.drain()
            if isinstance(event, h11.EndOfMessage):
                return

    async def _receive_response(
        self,
        outbound: h11.Connection,
        upstream_reader: asyncio.StreamReader,
        inbound: h11.Connection,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            event = await self._read_event(outbound, upstream_reader)
            if isinstance(event, (h11.Response, h11.InformationalResponse)):
                if event.status_code == HTTPStatus.SWITCHING_PROTOCOLS:
                    message = "HTTP Upgrade is unsupported; use CONNECT"
                    raise ProxyError(message)
                event = type(event)(
                    status_code=event.status_code,
                    reason=event.reason,
                    headers=headers_for_forwarding(event.headers),
                )
            elif not isinstance(event, (h11.Data, h11.EndOfMessage)):
                message = "Incomplete upstream response"
                raise ProxyError(message)
            writer.write(inbound.send(event))
            await writer.drain()
            if isinstance(event, h11.EndOfMessage):
                return

    async def _forward_http(
        self,
        request: h11.Request,
        inbound: h11.Connection,
        client: StreamPair,
        upstream: StreamPair,
        target: tuple[str, str],
    ) -> None:
        reader, writer = client
        upstream_reader, upstream_writer = upstream
        path, authority = target
        outbound = h11.Connection(h11.CLIENT)
        upstream_writer.write(
            outbound.send(
                h11.Request(
                    method=request.method,
                    target=path.encode("ascii"),
                    headers=headers_for_forwarding(request.headers, host=authority),
                ),
            ),
        )
        await upstream_writer.drain()

        tasks = [
            asyncio.create_task(
                self._send_body(inbound, reader, outbound, upstream_writer),
            ),
            asyncio.create_task(
                self._receive_response(outbound, upstream_reader, inbound, writer),
            ),
        ]
        try:
            # An upstream rejection may finish before the request body.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[1] not in done:
                await tasks[1]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._closing or len(self._tasks) >= self.config.http_proxy.max_connections:
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n",
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream_writer = None
        inbound = h11.Connection(h11.SERVER)
        connected = False
        status = HTTPStatus.BAD_REQUEST
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                self.config.http_proxy.idle_timeout_seconds,
            )
            inbound.receive_data(raw)
            request = _initial_request(inbound)
            connect = request.method == b"CONNECT"
            host, port, path = destination(request.target.decode("ascii"), connect)
            headers_for_forwarding(request.headers)  # Validate before connecting.
            status = HTTPStatus.SERVICE_UNAVAILABLE
            connection_id = self._require_active_tunnel()
            status = HTTPStatus.BAD_GATEWAY
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._socks(host, port, connection_id),
                self.config.http_proxy.connect_timeout_seconds,
            )
            self._require_active_tunnel(connection_id)
            # Counted once the tunnel accepts it: a forward and a CONNECT are
            # each one request over the connection, and the encrypted requests
            # inside a CONNECT tunnel cannot be seen from here. The ownership
            # gate above protects forwarding as well as the manager's totals.
            self.counter.record()
            if connect:
                writer.write(
                    inbound.send(
                        h11.Response(
                            status_code=200,
                            headers=[],
                            reason=b"Connection Established",
                        ),
                    ),
                )
                await writer.drain()
                connected = True
                await self._relay_connect(
                    reader,
                    writer,
                    upstream_reader,
                    upstream_writer,
                )
            else:
                authority = (
                    f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
                )
                if port != HTTP_PORT:
                    authority += f":{port}"
                await self._forward_http(
                    request,
                    inbound,
                    (reader, writer),
                    (upstream_reader, upstream_writer),
                    (path, authority),
                )
        except (
            OSError,
            ValueError,
            ProxyError,
            h11.ProtocolError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            # Never append an HTTP error after a response or tunnel has begun.
            if not connected and inbound.our_state in (h11.IDLE, h11.SEND_RESPONSE):
                await self._send_error(writer, status)
        finally:
            await self._close_streams(upstream_writer, writer)
            self._tasks.discard(task)

    async def _send_error(self, writer: asyncio.StreamWriter, status: int) -> None:
        reason = HTTPStatus(status).phrase.encode("ascii")
        writer.write(
            b"HTTP/1.1 "
            + str(status).encode()
            + b" "
            + reason
            + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        )
        with suppress(OSError):
            await writer.drain()

    async def _close_streams(self, *streams: asyncio.StreamWriter | None) -> None:
        for stream in streams:
            if stream:
                stream.close()
                with suppress(OSError):
                    await stream.wait_closed()


def _initial_request(connection: h11.Connection) -> h11.Request:
    """Require a parsed request before opening any upstream connection."""
    request = connection.next_event()
    if not isinstance(request, h11.Request):
        message = "Invalid request"
        raise ProxyError(message)
    return request


def _validate_socks_auth(reply: bytes) -> None:
    """Require SOCKS5 with unauthenticated access on the local tunnel socket."""
    if reply != b"\x05\x00":
        message = "SOCKS authentication negotiation failed"
        raise ProxyError(message)


def _socks_address(host: str) -> bytes:
    """Encode an IP literal or IDNA hostname for a SOCKS5 CONNECT request."""
    try:
        address = ip_address(host)
    except ValueError:
        name = host.encode("idna")
        if not 1 <= len(name) <= MAX_HOSTNAME_BYTES:
            message = "Destination hostname too long"
            raise ProxyError(message) from None
        return b"\x03" + bytes([len(name)]) + name
    else:
        prefix = b"\x01" if isinstance(address, IPv4Address) else b"\x04"
        return prefix + address.packed


async def _read_socks_reply(reader: asyncio.StreamReader) -> None:
    """Validate CONNECT success and consume the SOCKS reply's bound address."""
    reply = await reader.readexactly(4)
    if reply[:3] != b"\x05\x00\x00":
        message = "SOCKS destination connection failed"
        raise ProxyError(message)
    if reply[3] == SOCKS_IPV4:
        length = 4
    elif reply[3] == SOCKS_IPV6:
        length = 16
    elif reply[3] == SOCKS_DOMAIN:
        length = (await reader.readexactly(1))[0]
    else:
        message = "Invalid SOCKS address type"
        raise ProxyError(message)
    await reader.readexactly(length + 2)


async def _first_completed(
    tasks: Sequence[asyncio.Task[None]],
) -> set[asyncio.Task[None]]:
    """Wait for the first bridge task and propagate any completed task failures."""
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        task.result()
    return done
