"""Verify HTTP forwarding, SOCKS boundaries, TLS, and connection accounting."""

import asyncio
import ipaddress
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

import httpx
import pytest
from pydantic import ValidationError

from tunnel_switch.config import Config
from tunnel_switch.http_proxy import HTTPProxyServer

type Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class Network(Protocol):
    """Describe the loopback server factory yielded by the network fixture."""

    async def __call__(
        self,
        handler: Handler,
        *,
        ssl: ssl.SSLContext | None = None,
    ) -> int:
        """Start a managed loopback server and return its ephemeral port."""
        ...


@pytest.fixture
async def network() -> AsyncIterator[Network]:
    """Own loopback listeners, client writers, and handler tasks until teardown."""
    servers: list[asyncio.Server] = []
    tasks: set[asyncio.Task[object]] = set()
    writers: set[asyncio.StreamWriter] = set()

    async def start(handler: Handler, *, ssl: ssl.SSLContext | None = None) -> int:
        async def accept(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            task = asyncio.current_task()
            if task is not None:
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

        server = await asyncio.start_server(accept, "127.0.0.1", 0, ssl=ssl)
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
async def bridge(
    network: Network,
    *,
    rejected: bool = False,
    active: Callable[[], bool] | None = None,
    **settings: bool | float,
) -> AsyncIterator[tuple[int, list[tuple[str, int]], HTTPProxyServer]]:
    """Run the production HTTP proxy through a controlled SOCKS endpoint."""
    destinations: list[tuple[str, int]] = []

    async def socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(3) == b"\x05\x01\x00", "Invalid SOCKS greeting"
        writer.write(b"\x05\x00")
        await writer.drain()
        head = await reader.readexactly(4)
        assert head[:3] == b"\x05\x01\x00", "Invalid SOCKS CONNECT request"
        domain_address = 3
        if head[3] == domain_address:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode(
                "ascii",
            )
        else:
            host = str(
                ipaddress.ip_address(
                    await reader.readexactly(4 if head[3] == 1 else 16),
                ),
            )
        port = int.from_bytes(await reader.readexactly(2), "big")
        destinations.append((host, port))
        writer.write(
            b"\x05"
            + (b"\x05" if rejected else b"\x00")
            + b"\x00\x01\x7f\x00\x00\x01\x00\x00",
        )
        await writer.drain()
        if rejected:
            return
        # Resolve domain destinations here, demonstrating that the bridge sends remote DNS names.
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1",
            port,
        )

        async def pump(
            source: asyncio.StreamReader,
            target: asyncio.StreamWriter,
        ) -> None:
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
            if target.can_write_eof():
                target.write_eof()

        relays = [
            asyncio.create_task(pump(reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, writer)),
        ]
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
    cfg = Config.model_validate(
        {
            "servers": [{"name": "one", "host": "example.com", "user": "tunnel"}],
            "proxy": {"ip": "127.0.0.1", "port": socks_port},
            "http_proxy": settings,
            "start_on_launch": False,
        },
    )
    cfg.http_proxy.port = 0  # Ask the OS for an ephemeral listener only in tests.
    proxy = HTTPProxyServer(cfg, active=active)
    await proxy.start()
    port = proxy.server.sockets[0].getsockname()[1]
    try:
        yield port, destinations, proxy
    finally:
        await proxy.close()


async def test_http_get_rewrites_target_and_strips_proxy_credentials(
    network: Network,
) -> None:
    """Rewrite absolute targets and remove proxy credentials and hop headers."""
    requests = []

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
        await writer.drain()

    origin_port = await network(origin)
    async with bridge(network) as (port, destinations, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET http://example.test:{origin_port}/path?q=yes HTTP/1.1\r\nHost: wrong.example\r\nProxy-Authorization: Basic private\r\nConnection: keep-alive, x-remove\r\nX-Remove: private\r\n\r\n".encode(),
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await writer.wait_closed()
        assert response.endswith(b"hello"), "Response body was not forwarded"
        assert b"200" in response, "Origin success status was not forwarded"
        assert requests[0].startswith(b"GET /path?q=yes HTTP/1.1\r\n"), (
            "Target was not rewritten"
        )
        assert (
            f"Host: example.test:{origin_port}".encode().lower() in requests[0].lower()
        ), "Host header does not match target"
        assert b"private" not in requests[0], "Private proxy headers leaked upstream"
        assert b"wrong.example" not in requests[0], (
            "Original Host header leaked upstream"
        )
        assert destinations == [("example.test", origin_port)], (
            "SOCKS destination changed"
        )


async def test_http_streaming_chunked_post_and_response(network: Network) -> None:
    """Stream chunked request and response bodies without changing their payloads."""
    received = []

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        body = await reader.readuntil(b"0\r\n\r\n")
        assert b"first" in body, "First upload chunk was lost"
        assert b"second" in body, "Second upload chunk was lost"
        writer.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
        )
        await writer.drain()

    origin_port = await network(origin)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        await asyncio.sleep(0.01)
        yield b"second"

    async with (
        bridge(network) as (port, _, _),
        httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}",
            trust_env=False,
        ) as client,
    ):
        response = await client.post(
            f"http://127.0.0.1:{origin_port}/upload",
            content=chunks(),
        )
        assert response.status_code == httpx.codes.OK, "Chunked request failed"
        assert response.text == "hello", "Chunked response body changed"
    assert b"transfer-encoding: chunked" in received[0].lower(), (
        "Chunked framing was lost"
    )


async def test_expect_continue(network: Network) -> None:
    """Forward the interim response before the client sends its expected body."""

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
        assert await reader.readexactly(4) == b"body", "Expected request body changed"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()

    origin_port = await network(origin)
    async with bridge(network) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST http://127.0.0.1:{origin_port}/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nExpect: 100-continue\r\n\r\n".encode(),
        )
        await writer.drain()
        assert b"100" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2), (
            "Interim Continue response missing"
        )
        writer.write(b"body")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).endswith(b"ok"), (
            "Final response missing"
        )
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("streamed", [False, True])
async def test_connect_transports_real_tls(
    network: Network,
    tmp_path: Path,
    *,
    streamed: bool,
) -> None:
    """Carry a verified TLS session through CONNECT, including slow streaming."""
    key, certificate = tmp_path / "key.pem", tmp_path / "cert.pem"
    executable = shutil.which("openssl")
    if executable is None or not Path(executable).is_absolute():
        pytest.fail("An absolute OpenSSL executable is required for the TLS test")
    await asyncio.to_thread(
        subprocess.run,
        [
            executable,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(
            b"GET /secure HTTP/1.1"
        ), "TLS request target changed"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n")
        await writer.drain()
        for byte in b"secure":
            writer.write(bytes([byte]))
            await writer.drain()
            if streamed:
                await asyncio.sleep(0.05)

    origin_port = await network(origin, ssl=context)
    trusted = ssl.create_default_context(cafile=str(certificate))
    async with bridge(network, idle_timeout_seconds=0.2 if streamed else 120) as (
        port,
        destinations,
        _,
    ):
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}",
            verify=trusted,
            trust_env=False,
        ) as client:
            response = await client.get(f"https://localhost:{origin_port}/secure")
            assert response.text == "secure", "TLS response payload changed"
            assert response.status_code == httpx.codes.OK, "TLS origin request failed"
        assert destinations == [("localhost", origin_port)], (
            "CONNECT destination changed"
        )


async def test_connect_half_close_receives_remaining_upstream_data(
    network: Network,
) -> None:
    """Keep receiving upstream data after the client half-closes its upload."""

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        data = await reader.read()
        writer.write(data.upper())
        await writer.drain()

    origin_port = await network(origin)
    async with bridge(network) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
        )
        await writer.drain()
        assert b"200" in await reader.readuntil(b"\r\n\r\n"), "CONNECT failed"
        writer.write(b"hello")
        await writer.drain()
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 2) == b"HELLO", (
            "Half-close lost the upstream response"
        )
        writer.close()
        await writer.wait_closed()


async def test_socks_failure_returns_502_without_direct_fallback(
    network: Network,
) -> None:
    """Report SOCKS rejection for HTTP and HTTPS rather than bypassing the tunnel."""
    async with (
        bridge(network, rejected=True) as (port, _, _),
        httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}",
            trust_env=False,
        ) as client,
    ):
        assert (
            await client.get("http://example.test/")
        ).status_code == httpx.codes.BAD_GATEWAY, "SOCKS failure did not produce 502"
        with pytest.raises(httpx.ProxyError, match="502"):
            await client.get("https://example.test/")


@pytest.mark.parametrize(
    "wire_request",
    [
        b"GET /relative HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"CONNECT localhost:0 HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"POST http://example.test/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"CONNECT localhost HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET http://user:password@example.test/ HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"POST http://example.test/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n",
        b"GET http://example.test/ HTTP/1.1\r\nHost: localhost\r\nConnection: content-length\r\n\r\n",
        b"GET http://example.test/ HTTP/1.1\r\nHost: localhost\r\nX-Large: "
        + b"a" * 2048
        + b"\r\n\r\n",
    ],
)
async def test_invalid_requests_rejected_before_socks(
    network: Network,
    wire_request: bytes,
) -> None:
    """Reject malformed or unsafe HTTP requests without opening a SOCKS connection."""
    async with bridge(network, header_limit_bytes=1024) as (port, destinations, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(wire_request)
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 400"), (
            "Invalid request was not rejected"
        )
        assert destinations == [], "Invalid request reached SOCKS"
        writer.close()
        await writer.wait_closed()


async def test_limit_and_shutdown_close_active_connections(network: Network) -> None:
    """Enforce the client limit and close every active tunnel during shutdown."""

    async def origin(
        reader: asyncio.StreamReader,
        _writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read()

    origin_port = await network(origin)
    async with bridge(network, max_connections=1) as (port, _, proxy):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"CONNECT localhost:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
        )
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
        assert (await second_reader.read()).startswith(b"HTTP/1.1 503"), (
            "Connection limit did not reject the second client"
        )
        second_writer.close()
        await second_writer.wait_closed()
        await proxy.close()
        assert await asyncio.wait_for(reader.read(), 2) == b"", (
            "Shutdown left the tunnel open"
        )
        assert not proxy._tasks, "Shutdown left proxy tasks running"
        writer.close()
        await writer.wait_closed()


def test_listener_collision_validation() -> None:
    """Reject listeners colliding directly or through a wildcard bind."""
    document = {
        "servers": [{"name": "one", "host": "example.com", "user": "tunnel"}],
        "proxy": {"port": 1080},
        "http_proxy": {"port": 1080},
    }
    with pytest.raises(ValidationError, match="different sockets"):
        Config.model_validate(document)
    document["http_proxy"] = {"ip": "0.0.0.0", "port": 8765}
    with pytest.raises(ValidationError, match="different sockets"):
        Config.model_validate(document)


@pytest.mark.parametrize("direction", ["download", "upload"])
async def test_connect_active_stream_survives_quiet_opposite_direction(
    network: Network,
    direction: str,
) -> None:
    """Treat activity in either direction as keeping the entire tunnel alive."""
    chunk = b"tick"
    count = 10

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if direction == "download":
            assert await reader.readexactly(5) == b"start", (
                "Download start marker changed"
            )
            for _ in range(count):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0.05)
        else:
            assert await reader.read() == chunk * count, "Active upload lost chunks"
            writer.write(b"done")
            await writer.drain()

    origin_port = await network(origin)
    async with bridge(network, idle_timeout_seconds=0.2) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                f"CONNECT localhost:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
            )
            await writer.drain()
            assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2), (
                "CONNECT failed"
            )
            if direction == "download":
                writer.write(b"start")
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 2) == chunk * count, (
                    "Quiet upload direction interrupted active download"
                )
            else:
                for _ in range(count):
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0.05)
                writer.write_eof()
                assert await asyncio.wait_for(reader.read(), 2) == b"done", (
                    "Quiet download direction interrupted active upload"
                )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def test_connect_closes_when_both_directions_are_idle(network: Network) -> None:
    """Close a CONNECT tunnel once neither side is sending data."""

    async def origin(
        reader: asyncio.StreamReader,
        _writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read()

    origin_port = await network(origin)
    async with bridge(network, idle_timeout_seconds=0.2) as (port, _, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                f"CONNECT localhost:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
            )
            await writer.drain()
            assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2), (
                "CONNECT failed"
            )
            assert await asyncio.wait_for(reader.read(), 2) == b"", (
                "Idle tunnel remained open"
            )
        finally:
            writer.close()
            await writer.wait_closed()


async def test_requests_are_counted_once_the_tunnel_carries_them(
    network: Network,
) -> None:
    """Count one HTTP request and one CONNECT request carried through SOCKS."""

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            # A CONNECT tunnel closes on us without sending a request.
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()

    origin_port = await network(origin)
    async with bridge(network) as (port, _, proxy):
        assert proxy.counter.current == 0, "Fresh proxy counter was not zero"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET http://example.test:{origin_port}/one HTTP/1.1\r\nHost: example.test\r\n\r\n".encode(),
        )
        await writer.drain()
        assert (await asyncio.wait_for(reader.readuntil(b"ok"), 2)).endswith(b"ok"), (
            "HTTP request did not finish"
        )
        writer.close()
        await writer.wait_closed()
        # A CONNECT counts as one request as well: the requests inside its
        # tunnel are encrypted, so the proxy cannot see them.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
        )
        await writer.drain()
        assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2), (
            "CONNECT failed"
        )
        writer.close()
        await writer.wait_closed()
        expected_requests = 2
        assert proxy.counter.current == expected_requests, (
            "HTTP and CONNECT were not counted exactly once"
        )


async def test_requests_the_tunnel_never_carried_are_not_counted(
    network: Network,
) -> None:
    """Exclude refused tunnels and requests rejected before SOCKS from accounting."""
    async with bridge(network, rejected=True) as (port, _, proxy):
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}",
            trust_env=False,
        ) as client:
            assert (
                await client.get("http://example.test/")
            ).status_code == httpx.codes.BAD_GATEWAY, (
                "Refused SOCKS request did not fail"
            )
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /relative HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 400"), (
            "Relative request was not rejected"
        )
        writer.close()
        await writer.wait_closed()
        # Neither a refused tunnel nor a request rejected before it counts.
        assert proxy.counter.current == 0, "Uncarried requests incremented the counter"


async def test_requests_are_only_counted_while_an_adopted_tunnel_serves(
    network: Network,
) -> None:
    """Reject unadopted tunnel traffic and count successful adopted traffic once."""

    async def origin(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()

    origin_port = await network(origin)
    request = f"GET http://example.test:{origin_port}/ HTTP/1.1\r\nHost: example.test\r\n\r\n".encode()

    # An unrelated SOCKS listener must never receive an unadopted tunnel's traffic.
    async with bridge(network, active=lambda: False) as (port, destinations, proxy):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).startswith(b"HTTP/1.1 503"), (
            "Unadopted tunnel request did not fail closed"
        )
        writer.close()
        await writer.wait_closed()
        assert proxy.counter.current == 0, "Unadopted tunnel traffic was counted"
        assert destinations == [], (
            "Unadopted tunnel reached an unrelated SOCKS listener"
        )

    # The adopted tunnel is running again: the same request counts once.
    async with bridge(network, active=lambda: True) as (port, _, proxy):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        assert (await asyncio.wait_for(reader.readuntil(b"ok"), 2)).endswith(b"ok"), (
            "Adopted listener failed to forward"
        )
        writer.close()
        await writer.wait_closed()
        assert proxy.counter.current == 1, "Adopted tunnel request was not counted once"


@pytest.mark.parametrize("method", ["GET", "CONNECT"])
async def test_inactive_tunnel_never_connects_to_rogue_socks_listener(
    network: Network,
    method: str,
) -> None:
    """Fail closed before contacting a rogue SOCKS listener for HTTP or CONNECT."""
    accepted: list[asyncio.StreamWriter] = []

    async def rogue(
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        accepted.append(writer)

    socks_port = await network(rogue)
    config = Config.model_validate(
        {
            "servers": [{"host": "example.test"}],
            "proxy": {"ip": "127.0.0.1", "port": socks_port},
            "start_on_launch": False,
        },
    )
    config.http_proxy.port = 0
    proxy = HTTPProxyServer(config, active=lambda: False)
    await proxy.start()
    if proxy.server is None:
        pytest.fail("HTTP proxy did not create a listener")
    port = proxy.server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            target = (
                "example.test:443" if method == "CONNECT" else "http://example.test/"
            )
            writer.write(
                f"{method} {target} HTTP/1.1\r\nHost: example.test\r\n\r\n".encode(),
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            assert response.startswith(b"HTTP/1.1 503"), (
                "Inactive tunnel must return service unavailable"
            )
            assert accepted == [], (
                "Inactive proxy connected to the rogue SOCKS listener"
            )
            assert proxy.counter.current == 0, (
                "Rejected inactive request incremented accounting"
            )
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.close()


@pytest.mark.parametrize("phase", ["connect", "authentication", "reply"])
async def test_tunnel_generation_changes_abort_socks_negotiation(
    network: Network,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Reject a stale stream even when the new tunnel is already adopted."""
    generation = ["original-connection"]
    observed: list[bytes] = []
    finished = asyncio.Event()

    async def socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            greeting = await reader.readexactly(3)
            observed.append(greeting)
            if phase == "authentication":
                generation[0] = "replacement-connection"
            writer.write(b"\x05\x00")
            await writer.drain()
            header = await reader.readexactly(4)
            observed.append(header)
            length = (await reader.readexactly(1))[0]
            observed.append(await reader.readexactly(length + 2))
            if phase == "reply":
                generation[0] = "replacement-connection"
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            observed.append(await reader.read())
        except asyncio.IncompleteReadError as exc:
            observed.append(exc.partial)
        finally:
            finished.set()

    socks_port = await network(socks)
    config = Config.model_validate(
        {
            "servers": [{"host": "example.test"}],
            "proxy": {"ip": "127.0.0.1", "port": socks_port},
            "state_file": None,
            "start_on_launch": False,
        }
    )
    config.http_proxy.port = 0
    proxy = HTTPProxyServer(
        config,
        active=lambda: True,
        connection_id=lambda: generation[0],
    )
    open_connection = asyncio.open_connection

    async def connect(host: str, port: int):
        streams = await open_connection(host, port)
        if phase == "connect" and port == socks_port:
            generation[0] = "replacement-connection"
        return streams

    monkeypatch.setattr(asyncio, "open_connection", connect)
    await proxy.start()
    if proxy.server is None:
        pytest.fail("HTTP proxy did not create a listener")
    port = proxy.server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            await asyncio.wait_for(finished.wait(), 2)
            assert response.startswith(b"HTTP/1.1 502"), (
                "A replaced tunnel must reject the stale SOCKS stream"
            )
            assert b"GET " not in b"".join(observed), (
                "A stale SOCKS stream received forwarded HTTP traffic"
            )
            assert proxy.counter.current == 0, "Rejected stale traffic was counted"
            if phase != "reply":
                assert b"example.test" not in b"".join(observed), (
                    "A replaced tunnel received the destination before admission"
                )
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.close()
