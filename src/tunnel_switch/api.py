from contextlib import asynccontextmanager
import asyncio
import logging
import secrets
import signal
from typing import Callable, Sequence
from fastapi import Depends, FastAPI, Header, HTTPException
from .config import Config
from .http_proxy import HTTPProxyServer
from .reload import ConfigReloader, ReloadOutcome
from .tunnel import RotationRejected, TunnelFailure, TunnelManager

logger = logging.getLogger(__name__)


def _build_reloader(app, config, config_path, on_reload, prepare, manager):
    """Create the reloader, or None when the service was given no YAML file.

    ``app.state`` is one of the reload targets, so the app must exist first;
    the lifespan reads the reloader back from there once it starts.
    """
    if config_path is None:
        return None
    return ConfigReloader(
        config_path,
        config,
        targets=[manager, app.state.http_proxy, app.state],
        hooks=on_reload,
        prepare=prepare,
        busy=lambda: manager.busy,
    )


def _start_reload(app, loop, stop, watch):
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
            signal.SIGHUP, lambda: asyncio.create_task(reloader.reload_now())
        )
    except (RuntimeError, ValueError) as exc:
        logger.debug("SIGHUP reload trigger unavailable: %s", exc)
    return poller


async def _stop_reload(app, loop, stop, poller):
    if app.state.reloader is not None:
        loop.remove_signal_handler(signal.SIGHUP)
    if poller is not None:
        stop.set()
        await poller


async def _close(proxy, manager):
    try:
        await proxy.close()
    finally:
        await manager.close()


def _lifespan(config, manager, proxy, watch):
    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        app.state.loop = loop
        stop = asyncio.Event()
        poller = _start_reload(app, loop, stop, watch)
        try:
            if config.start_on_launch:
                await manager.rotate()
            await proxy.start()
            yield
        finally:
            try:
                await _stop_reload(app, loop, stop, poller)
            finally:
                await _close(proxy, manager)

    return lifespan


def _authorizer(app, token):
    def authorize(authorization: str | None = Header(default=None)):
        # The current token is read per request so a reload can rotate it.
        expected = f"Bearer {app.state.config.api.token or token}".encode()
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), expected
        ):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    return authorize


def _reload_status(app, config_path, watch):
    reloader = app.state.reloader
    return {
        "watching": reloader is not None and watch,
        "path": str(config_path) if config_path is not None else None,
        "last_reload": reloader.last_reload if reloader else None,
        "last_error": reloader.last_error if reloader else None,
        "deferred": list(reloader.deferred) if reloader else [],
    }


def _register_routes(app, manager, authorize, config_path, watch):
    @app.get("/status", dependencies=[Depends(authorize)])
    async def status():
        current = app.state.config
        return {
            **manager.status(),
            "http_proxy": {
                "enabled": current.http_proxy.enabled,
                "ip": str(current.http_proxy.ip),
                "port": current.http_proxy.port,
            },
            "config_reload": _reload_status(app, config_path, watch),
        }

    @app.post(
        "/rotate",
        dependencies=[Depends(authorize)],
        responses={
            429: {"description": "Rotation already running or cooling down"},
            503: {"description": "SSH tunnel could not be established"},
        },
    )
    async def rotate():
        reloader = app.state.reloader
        # A rotation creates a fresh ssh process, so a deferred SOCKS bind
        # becomes live there — but only once the rotation is admitted: a
        # rejected request must leave the still-running bind untouched.
        pending = reloader.apply_pending_binds if reloader is not None else None
        try:
            return await manager.rotate(pending)
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
                "description": "The service was started without a configuration file"
            },
            429: {"description": "Rotation already running or cooling down"},
        },
    )
    async def reload():
        reloader = app.state.reloader
        if reloader is None:
            raise HTTPException(
                status_code=404, detail="No configuration file was given to the service"
            )
        if manager.busy:
            raise HTTPException(
                status_code=429,
                detail="Tunnel rotation already in progress",
                headers={"Retry-After": "1"},
            )
        return (await reloader.reload_now()).payload()


def create_app(
    config: Config,
    token: str,
    manager: TunnelManager | None = None,
    *,
    config_path=None,
    watch: bool = True,
    on_reload: Sequence[Callable[[ReloadOutcome], None]] = (),
    prepare: Callable[[Config], None] | None = None,
) -> FastAPI:
    if not token:
        raise ValueError("API token must not be empty")
    manager = manager or TunnelManager(config)
    proxy = HTTPProxyServer(config)
    app = FastAPI(
        title="Tunnel Switch", lifespan=_lifespan(config, manager, proxy, watch)
    )
    app.state.manager = manager
    app.state.http_proxy = proxy
    app.state.config = config
    app.state.loop = None
    # Every reload swaps the config attribute of these objects, so the API, the
    # tunnel manager and the HTTP bridge switch configuration as one reference.
    app.state.reloader = _build_reloader(
        app, config, config_path, on_reload, prepare, manager
    )
    _register_routes(app, manager, _authorizer(app, token), config_path, watch)
    return app
