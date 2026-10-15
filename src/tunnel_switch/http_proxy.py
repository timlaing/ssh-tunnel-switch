"""HTTP/1.1 forward proxy and CONNECT bridge over the managed SSH SOCKS5 socket."""
import asyncio
from ipaddress import ip_address
from urllib.parse import urlsplit
import h11
from .config import Config


class ProxyError(Exception):
    pass


def destination(target: str, connect: bool = False) -> tuple[str, int, str]:
    url = urlsplit(f"//{target}" if connect else target)
    if url.username is not None or url.password is not None or url.fragment:
        raise ValueError("Invalid proxy target")
    if not url.hostname or (not connect and url.scheme != "http"):
        raise ValueError("Use an absolute HTTP URL or CONNECT for HTTPS")
    if connect and (url.path or url.query or url.port is None):
        raise ValueError("CONNECT requires host:port")
    port = url.port if url.port is not None else 80
    if not 1 <= port <= 65535:
        raise ValueError("Invalid destination port")
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    return url.hostname, port, path


def headers_for_forwarding(headers, *, host: str | None = None):
    names = {name.lower() for name, _ in headers}
    if {b"content-length", b"transfer-encoding"} <= names:
        raise ValueError("Ambiguous HTTP body framing")
    connection_tokens = {
        item.strip().lower() for name, value in headers if name.lower() == b"connection"
        for item in value.split(b",")
    }
    if connection_tokens & {b"host", b"content-length", b"transfer-encoding"}:
        raise ValueError("Invalid Connection header")
    stripped = connection_tokens | {b"connection", b"proxy-connection", b"proxy-authorization",
                                    b"proxy-authenticate", b"keep-alive", b"upgrade"}
    if host is not None:
        stripped.add(b"host")
    result = [(name, value) for name, value in headers if name.lower() not in stripped]
    if host is not None:
        result.append((b"host", host.encode("ascii")))
    result.append((b"connection", b"close"))
    return result


class HTTPProxyServer:
    def __init__(self, config: Config):
        self.config = config
        self.server = None
        self._tasks = set()
        self._closing = False

    async def start(self):
        if self.config.http_proxy.enabled:
            self.server = await asyncio.start_server(
                self._accept, str(self.config.http_proxy.ip), self.config.http_proxy.port,
                limit=self.config.http_proxy.header_limit_bytes)

    async def close(self):
        self._closing = True
        if self.server:
            self.server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()

    async def _socks(self, host: str, port: int):
        proxy_ip = str(self.config.proxy.ip)
        if proxy_ip == "0.0.0.0":
            proxy_ip = "127.0.0.1"
        elif proxy_ip == "::":
            proxy_ip = "::1"
        reader, writer = await asyncio.open_connection(proxy_ip, self.config.proxy.port)
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            if await reader.readexactly(2) != b"\x05\x00":
                raise ProxyError("SOCKS authentication negotiation failed")
            try:
                address = ip_address(host)
                encoded = (b"\x01" if address.version == 4 else b"\x04") + address.packed
            except ValueError:
                name = host.encode("idna")
                if not 1 <= len(name) <= 255:
                    raise ProxyError("Destination hostname too long")
                encoded = b"\x03" + bytes([len(name)]) + name
            writer.write(b"\x05\x01\x00" + encoded + port.to_bytes(2, "big"))
            await writer.drain()
            reply = await reader.readexactly(4)
            if reply[:3] != b"\x05\x00\x00":
                raise ProxyError("SOCKS destination connection failed")
            if reply[3] == 1:
                length = 4
            elif reply[3] == 4:
                length = 16
            elif reply[3] == 3:
                length = (await reader.readexactly(1))[0]
            else:
                raise ProxyError("Invalid SOCKS address type")
            await reader.readexactly(length + 2)
            return reader, writer
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def _read_event(self, connection, reader):
        while True:
            event = connection.next_event()
            if event is not h11.NEED_DATA:
                return event
            data = await asyncio.wait_for(reader.read(65536), self.config.http_proxy.idle_timeout_seconds)
            connection.receive_data(data)

    async def _relay_connect(self, client_reader, client_writer, upstream_reader, upstream_writer):
        loop = asyncio.get_running_loop()
        last_activity = loop.time()

        async def pump(reader, writer):
            nonlocal last_activity
            while data := await reader.read(65536):
                last_activity = loop.time()
                writer.write(data)
                await writer.drain()
                last_activity = loop.time()
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()

        async def watch_idle():
            # HTTPS requests can be quiet in one direction throughout a streamed
            # response. Only silence across the entire tunnel is inactivity.
            while True:
                remaining = self.config.http_proxy.idle_timeout_seconds - (loop.time() - last_activity)
                if remaining <= 0:
                    raise TimeoutError("CONNECT tunnel inactive")
                await asyncio.sleep(remaining)

        tasks = [asyncio.create_task(pump(client_reader, upstream_writer)),
                 asyncio.create_task(pump(upstream_reader, client_writer)),
                 asyncio.create_task(watch_idle())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[1] not in done:
                # Preserve client half-close: the upstream may still send its
                # response, but the shared inactivity deadline still applies.
                done, _ = await asyncio.wait(tasks[1:], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_body(self, inbound, reader, outbound, upstream_writer):
        while True:
            event = await self._read_event(inbound, reader)
            if not isinstance(event, (h11.Data, h11.EndOfMessage)):
                raise ProxyError("Incomplete HTTP request body")
            upstream_writer.write(outbound.send(event))
            await upstream_writer.drain()
            if isinstance(event, h11.EndOfMessage):
                return

    async def _receive_response(self, outbound, upstream_reader, inbound, writer):
        while True:
            event = await self._read_event(outbound, upstream_reader)
            if isinstance(event, (h11.Response, h11.InformationalResponse)):
                if event.status_code == 101:
                    raise ProxyError("HTTP Upgrade is unsupported; use CONNECT")
                event = type(event)(status_code=event.status_code, reason=event.reason,
                                   headers=headers_for_forwarding(event.headers))
            elif not isinstance(event, (h11.Data, h11.EndOfMessage)):
                raise ProxyError("Incomplete upstream response")
            writer.write(inbound.send(event))
            await writer.drain()
            if isinstance(event, h11.EndOfMessage):
                return


    async def _forward_http(self, request, inbound, reader, writer, upstream_reader, upstream_writer, path, authority):
        outbound = h11.Connection(h11.CLIENT)
        upstream_writer.write(outbound.send(h11.Request(
            method=request.method, target=path.encode("ascii"),
            headers=headers_for_forwarding(request.headers, host=authority))))
        await upstream_writer.drain()

        tasks = [asyncio.create_task(self._send_body(inbound, reader, outbound, upstream_writer)),
                 asyncio.create_task(self._receive_response(outbound, upstream_reader, inbound, writer))]
        try:
            # The response may finish before the request body (e.g. an upstream rejection).
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[1] not in done:
                await tasks[1]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(self, reader, writer):
        if self._closing or len(self._tasks) >= self.config.http_proxy.max_connections:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream_writer = None
        inbound = h11.Connection(h11.SERVER)
        connected = False
        status = 400
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.config.http_proxy.idle_timeout_seconds)
            inbound.receive_data(raw)
            request = inbound.next_event()
            if not isinstance(request, h11.Request):
                raise ValueError("Invalid request")
            connect = request.method == b"CONNECT"
            host, port, path = destination(request.target.decode("ascii"), connect)
            headers_for_forwarding(request.headers)  # Validate before connecting.
            status = 502
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._socks(host, port), self.config.http_proxy.connect_timeout_seconds)
            if connect:
                writer.write(inbound.send(h11.Response(status_code=200, headers=[], reason=b"Connection Established")))
                await writer.drain()
                connected = True
                await self._relay_connect(reader, writer, upstream_reader, upstream_writer)
            else:
                authority = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
                if port != 80:
                    authority += f":{port}"
                await self._forward_http(request, inbound, reader, writer, upstream_reader, upstream_writer, path, authority)
        except (OSError, ValueError, ProxyError, h11.ProtocolError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            # Never append an HTTP error after a response or tunnel has begun.
            if not connected and inbound.our_state in (h11.IDLE, h11.SEND_RESPONSE):
                await self._send_error(writer, status)
        finally:
            await self._close_streams(upstream_writer, writer)
            self._tasks.discard(task)

    async def _send_error(self, writer, status):
        reason = b"Bad Request" if status == 400 else b"Bad Gateway"
        writer.write(b"HTTP/1.1 " + str(status).encode() + b" " + reason
                     + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        try:
            await writer.drain()
        except OSError:
            pass

    async def _close_streams(self, *streams):
        for stream in streams:
            if stream:
                stream.close()
                try:
                    await stream.wait_closed()
                except OSError:
                    pass
