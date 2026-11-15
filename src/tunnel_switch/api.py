"""Expose authenticated tunnel control and configuration reload over HTTP."""

import asyncio
import logging
import secrets
import signal
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from starlette.requests import ClientDisconnect

from . import __version__
from .config import Config
from .counters import RequestCounter
from .http_proxy import HTTPProxyServer
from .reload import ConfigReloader, ReloadOutcome
from .tunnel import RotationRejected, TunnelFailure, TunnelManager

logger = logging.getLogger(__name__)


def _build_reloader(
    app: FastAPI,
    config: Config,
    config_path: Path | None,
    on_reload: Sequence[Callable[[ReloadOutcome], None]],
    prepare: Callable[[Config], None] | None,
) -> ConfigReloader | None:
    """Create the reloader, or None when the service was given no YAML file.

    ``app.state`` is one of the reload targets, so the app must exist first;
    the lifespan reads the reloader back from there once it starts.
    """
    if config_path is None:
        return None
    manager: TunnelManager = app.state.manager
    return ConfigReloader(
        config_path,
        config,
        targets=[manager, app.state.http_proxy, app.state],
        hooks=on_reload,
        prepare=prepare,
        busy=lambda: manager.busy,
    )


def _start_reload(
    app: FastAPI,
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    watch: bool,
) -> asyncio.Task[None] | None:
    """Start the config poller and the SIGHUP trigger; return the poll task."""
    reloader = app.state.reloader
    if reloader is None:
        return None
    poller = None
    if watch:
        poller = asyncio.create_task(reloader.run(stop))
    try:
        # A manual trigger for systemd's ExecReload, independent of polling.
        loop.add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(reloader.reload_now()),
        )
    except (RuntimeError, ValueError) as exc:
        logger.debug("SIGHUP reload trigger unavailable: %s", exc)
    return poller


async def _stop_reload(
    app: FastAPI,
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    poller: asyncio.Task[None] | None,
) -> None:
    if app.state.reloader is not None:
        loop.remove_signal_handler(signal.SIGHUP)
    if poller is not None:
        stop.set()
        await poller


async def _close(proxy: HTTPProxyServer, manager: TunnelManager) -> None:
    try:
        await proxy.close()
    finally:
        await manager.close()


def _lifespan(
    config: Config,
    manager: TunnelManager,
    proxy: HTTPProxyServer,
    *,
    watch: bool,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        app.state.loop = loop
        stop = asyncio.Event()
        poller = _start_reload(app, loop, stop, watch=watch)
        try:
            if config.start_on_launch:
                try:
                    await manager.rotate()
                except TunnelFailure:
                    if manager.config.reconnect_attempts == 0:
                        raise
                    logger.warning(
                        "Initial tunnel connection failed; "
                        "automatic recovery will retry",
                    )
            await proxy.start()
            reloader = app.state.reloader
            manager.start_monitor(
                reloader.apply_pending_binds if reloader is not None else None,
            )
            yield
        finally:
            try:
                await _stop_reload(app, loop, stop, poller)
            finally:
                await _close(proxy, manager)

    return lifespan


def _authorizer(app: FastAPI, token: str) -> Callable[..., None]:
    def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # The current token is read per request so a reload can rotate it.
        expected = f"Bearer {app.state.config.api.token or token}".encode()
        if authorization is None or not secrets.compare_digest(
            authorization.encode(),
            expected,
        ):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    return authorize


def _reload_status(
    app: FastAPI,
    config_path: Path | None,
    *,
    watch: bool,
) -> dict[str, bool | str | list[str] | None]:
    reloader = app.state.reloader
    return {
        "watching": reloader is not None and watch,
        "path": str(config_path) if config_path is not None else None,
        "last_reload": reloader.last_reload if reloader else None,
        "last_error": reloader.last_error if reloader else None,
        "deferred": list(reloader.deferred) if reloader else [],
    }


def _rotation_in_progress() -> HTTPException:
    """Build the retry response for a reload rejected during rotation."""
    return HTTPException(
        status_code=429,
        detail="Tunnel rotation already in progress",
        headers={"Retry-After": "1"},
    )


async def _request_reason(request: Request) -> object:
    """Read an optional rotation reason without requiring a JSON body."""
    try:
        body = await request.json()
        return body.get("reason") if isinstance(body, dict) else None
    except (ValueError, RuntimeError, ClientDisconnect):
        return None


def _register_routes(
    app: FastAPI,
    manager: TunnelManager,
    authorize: Callable[..., None],
    config_path: Path | None,
    *,
    watch: bool,
) -> None:
    @app.get("/status", dependencies=[Depends(authorize)])
    async def status() -> dict[str, object]:
        current = app.state.config
        return {
            **manager.status(),
            # Which build answers: the CLI flag, the panel title and this
            # field all read the same distribution metadata.
            "version": __version__,
            "http_proxy": {
                "enabled": current.http_proxy.enabled,
                "ip": str(current.http_proxy.ip),
                "port": current.http_proxy.port,
            },
            "config_reload": _reload_status(app, config_path, watch=watch),
        }

    @app.post(
        "/rotate",
        dependencies=[Depends(authorize)],
        responses={
            429: {"description": "Rotation already running or cooling down"},
            503: {"description": "SSH tunnel could not be established"},
        },
    )
    async def rotate(request: Request) -> dict[str, object]:
        reloader = app.state.reloader
        # A rotation creates a fresh ssh process, so a deferred SOCKS bind
        # becomes live there — but only once the rotation is admitted: a
        # rejected request must leave the still-running bind untouched.
        pending = reloader.apply_pending_binds if reloader is not None else None
        reason = await _request_reason(request)
        try:
            return await manager.rotate(pending, reason=reason)
        except RotationRejected as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except TunnelFailure as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post(
        "/reload",
        dependencies=[Depends(authorize)],
        responses={
            404: {
                "description": "The service was started without a configuration file",
            },
            429: {"description": "Rotation already running or cooling down"},
        },
    )
    async def reload() -> dict[str, bool | str | list[str] | int | None]:
        reloader = app.state.reloader
        if reloader is None:
            raise HTTPException(
                status_code=404,
                detail="No configuration file was given to the service",
            )
        if manager.busy:
            raise _rotation_in_progress()
        outcome = await reloader.reload_now()
        if outcome.refused:
            # The rotation began while the file was being read, past the entry
            # gate above: callers get the same 429 retry contract either way.
            raise _rotation_in_progress()
        return outcome.payload()


def create_app(
    config: Config,
    token: str,
    manager: TunnelManager | None = None,
    *,
    config_path: Path | None = None,
    watch: bool = True,
    on_reload: Sequence[Callable[[ReloadOutcome], None]] = (),
    prepare: Callable[[Config], None] | None = None,
    counter: RequestCounter | None = None,
) -> FastAPI:
    """Create the authenticated API with shared tunnel, proxy and reload state.

    Optional keyword arguments configure the YAML path, polling, reload hooks,
    per-run overrides and a request counter. A supplied manager owns its counter.
    """
    if not token:
        message = "API token must not be empty"
        raise ValueError(message)
    if manager is not None and counter is not None:
        message = (
            "Pass either a manager or a counter, not both: the proxy records "
            "into the counter of the manager it serves, so a separate "
            "counter would never see the requests"
        )
        raise ValueError(message)
    manager = manager or TunnelManager(config, counter=counter)
    # The proxy records into the manager's counter, so one total backs both the
    # status panel and GET /status no matter which of the two was built here,
    # and it asks the manager whether an adopted tunnel is running before it
    # counts, so traffic on the SOCKS port while no tunnel is ours does not
    # enter the totals.
    proxy = HTTPProxyServer(
        config,
        counter=manager.counter,
        active=manager.adopted,
        connection_id=manager.connection_id,
    )
    app = FastAPI(
        title="Tunnel Switch",
        lifespan=_lifespan(config, manager, proxy, watch=watch),
    )
    app.state.manager = manager
    app.state.http_proxy = proxy
    app.state.config = config
    app.state.loop = None
    # Every reload swaps the config attribute of these objects, so the API, the
    # tunnel manager and the HTTP bridge switch configuration as one reference.
    app.state.reloader = _build_reloader(
        app,
        config,
        config_path,
        on_reload,
        prepare,
    )
    _register_routes(
        app,
        manager,
        _authorizer(app, token),
        config_path,
        watch=watch,
    )
    return app
