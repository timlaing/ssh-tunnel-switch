import asyncio
from contextlib import asynccontextmanager
import ipaddress
import ssl
import subprocess
import httpx
import pytest
from pydantic import ValidationError
from tunnel_switch.config import Config
from tunnel_switch.http_proxy import HTTPProxyServer


@pytest.fixture
async def network():
    servers, tasks, writers = [], set(), set()

    async def start(handler, **kwargs):
        async def accept(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            writers.add(writer)
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                writers.discard(writer)
                tasks.discard(task)
        server = await asyncio.start_server(accept, "127.0.0.1", 0, **kwargs)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start
    for server in servers:
        server.close()
    for writer in list(writers):
        writer.close()
    pending = list(tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for server in servers:
        await server.wait_closed()


@asynccontextmanager
async def bridge(network, *, rejected=False, **settings):
    destinations = []

    async def socks(reader, writer):
        assert await reader.readexactly(3) == b"\x05\x01\x00"
        writer.write(b"\x05\x00")
        await writer.drain()
        head = await reader.readexactly(4)
        assert head[:3] == b"\x05\x01\x00"
        if head[3] == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode("ascii")
        else:
            host = str(ipaddress.ip_address(await reader.readexactly(4 if head[3] == 1 else 16)))
        port = int.from_bytes(await reader.readexactly(2), "big")
        destinations.append((host, port))
        writer.write(b"\x05" + (b"\x05" if rejected else b"\x00") + b"\x00\x01\x7f\x00\x00\x01\x00\x00")
        await writer.drain()
        if rejected:
            return
        # Resolve domain destinations here, demonstrating that the bridge sends remote DNS names.
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
        async def pump(source, target):
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
            if target.can_write_eof():
                target.write_eof()
        relays = [asyncio.create_task(pump(reader, upstream_writer)),
                  asyncio.create_task(pump(upstream_reader, writer))]
        try:
            done, _ = await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if relays[1] not in done:
                await relays[1]
        finally:
            for task in relays:
                task.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
            upstream_writer.close()
            await upstream_writer.wait_closed()

    socks_port = await network(socks)
    cfg = Config.model_validate({"servers": [{"name": "one", "host": "example.com", "user": "tunnel"}],
                                 "proxy": {"ip": "127.0.0.1", "port": socks_port},
                                 "http_proxy": settings, "start_on_launch": False})
    cfg.http_proxy.port = 0  # Ask the OS for an ephemeral listener only in tests.
    proxy = HTTPProxyServer(cfg)
    await proxy.start()
    port = proxy.server.sockets[0].getsockname()[1]
    try:
        yield port, destinations, proxy
    finally:
        await proxy.close()


async def test_http_get_rewrites_target_and_strips_proxy_credentials(network):
    requests = []
    async def origin(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
        await writer.drain()
    origin_port = await network(origin)
    async with bridge(network) as (port, destinations, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET http://example.test:{origin_port}/path?q=yes HTTP/1.1\r\nHost: wrong.example\r\nProxy-Authorization: Basic private\r\nConnection: keep-alive, x-remove\r\nX-Remove: private\r\n\r\n".encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await writer.wait_closed()
        assert response.endswith(b"hello") and b"200" in response
        assert requests[0].startswith(b"GET /path?q=yes HTTP/1.1\r\n")
        assert f"Host: example.test:{origin_port}".encode().lower() in requests[0].lower()
        assert b"private" not in requests[0] and b"wrong.example" not in requests[0]
        assert destinations == [("example.test", origin_port)]


async def test_http_streaming_chunked_post_and_response(network):
    received = []
    async def origin(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        body = await reader.readuntil(b"0\r\n\r\n")
        assert b"first" in body and b"second" in body
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
        await writer.drain()
    origin_port = await network(origin)
    async def chunks():
        yield b"first"
        await asyncio.sleep(0.01)
        yield b"second"
    async with bridge(network) as (port, _, _):
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{port}", trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{origin_port}/upload", content=chunks())
            assert response.status_code == 200 and response.text == "hello"
    assert b"transfer-encoding: chunked" in received[0].lower()


async def test_expect_continue(network):
    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
        assert await reader.readexactly(4) == b"body"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
    origin_port = await network(origin)
    async with bridge(network) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"POST http://127.0.0.1:{origin_port}/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nExpect: 100-continue\r\n\r\n".encode())
        await writer.drain()
        assert b"100" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        writer.write(b"body")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).endswith(b"ok")
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("streamed", [False, True])
async def test_connect_transports_real_tls(network, tmp_path, streamed):
    key, certificate = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    async def origin(reader, writer):
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"GET /secure HTTP/1.1")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n")
        await writer.drain()
        for byte in b"secure":
            writer.write(bytes([byte]))
            await writer.drain()
            if streamed:
                await asyncio.sleep(0.05)
    origin_port = await network(origin, ssl=context)
    trusted = ssl.create_default_context(cafile=str(certificate))
    async with bridge(network, idle_timeout_seconds=0.2 if streamed else 120) as (port, destinations, _):
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{port}", verify=trusted, trust_env=False) as client:
            response = await client.get(f"https://localhost:{origin_port}/secure")
            assert response.text == "secure" and response.status_code == 200
        assert destinations == [("localhost", origin_port)]


async def test_connect_half_close_receives_remaining_upstream_data(network):
    async def origin(reader, writer):
        data = await reader.read()
        writer.write(data.upper())
        await writer.drain()
    origin_port = await network(origin)
    async with bridge(network) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        assert b"200" in await reader.readuntil(b"\r\n\r\n")
        writer.write(b"hello")
        await writer.drain()
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 2) == b"HELLO"
        writer.close()
        await writer.wait_closed()


async def test_socks_failure_returns_502_without_direct_fallback(network):
    async with bridge(network, rejected=True) as (port, _, _):
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert (await client.get("http://example.test/")).status_code == 502
            with pytest.raises(httpx.ProxyError, match="502"):
                await client.get("https://example.test/")


@pytest.mark.parametrize("wire_request", [
    b"GET /relative HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"CONNECT localhost:0 HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"POST http://example.test/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"CONNECT localhost HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"GET http://user:password@example.test/ HTTP/1.1\r\nHost: localhost\r\n\r\n",
    b"POST http://example.test/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n",
    b"GET http://example.test/ HTTP/1.1\r\nHost: localhost\r\nConnection: content-length\r\n\r\n",
    b"GET http://example.test/ HTTP/1.1\r\nHost: localhost\r\nX-Large: " + b"a" * 2048 + b"\r\n\r\n",
])
async def test_invalid_requests_rejected_before_socks(network, wire_request):
    async with bridge(network, header_limit_bytes=1024) as (port, destinations, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(wire_request)
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 400")
        assert destinations == []
        writer.close()
        await writer.wait_closed()


async def test_limit_and_shutdown_close_active_connections(network):
    async def origin(reader, writer):
        await reader.read()
    origin_port = await network(origin)
    async with bridge(network, max_connections=1) as (port, _, proxy):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT localhost:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
        assert (await second_reader.read()).startswith(b"HTTP/1.1 503")
        second_writer.close()
        await second_writer.wait_closed()
        await proxy.close()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        assert not proxy._tasks
        writer.close()
        await writer.wait_closed()


def test_listener_collision_validation():
    document = {"servers": [{"name": "one", "host": "example.com", "user": "tunnel"}],
                "proxy": {"port": 1080}, "http_proxy": {"port": 1080}}
    with pytest.raises(ValidationError, match="different sockets"):
        Config.model_validate(document)
    document["http_proxy"] = {"ip": "0.0.0.0", "port": 8765}
    with pytest.raises(ValidationError, match="different sockets"):
        Config.model_validate(document)


@pytest.mark.parametrize("direction", ["download", "upload"])
async def test_connect_active_stream_survives_quiet_opposite_direction(network, direction):
    chunk = b"tick"
    count = 10

    async def origin(reader, writer):
        if direction == "download":
            assert await reader.readexactly(5) == b"start"
            for _ in range(count):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0.05)
        else:
            assert await reader.read() == chunk * count
            writer.write(b"done")
            await writer.drain()

    origin_port = await network(origin)
    async with bridge(network, idle_timeout_seconds=0.2) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(f"CONNECT localhost:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            if direction == "download":
                writer.write(b"start")
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 2) == chunk * count
            else:
                for _ in range(count):
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0.05)
                writer.write_eof()
                assert await asyncio.wait_for(reader.read(), 2) == b"done"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def test_connect_closes_when_both_directions_are_idle(network):
    async def origin(reader, writer):
        await reader.read()

    origin_port = await network(origin)
    async with bridge(network, idle_timeout_seconds=0.2) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(f"CONNECT localhost:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert await asyncio.wait_for(reader.read(), 2) == b""
        finally:
            writer.close()
            await writer.wait_closed()
