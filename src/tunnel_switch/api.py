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

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        app.state.loop = loop
        stop = asyncio.Event()
        reloader = app.state.reloader
        poller = None
        if reloader is not None:
            if watch:
                poller = asyncio.create_task(reloader.run(stop))
            try:
                # A manual trigger for systemd's ExecReload, independent of polling.
                loop.add_signal_handler(
                    signal.SIGHUP, lambda: asyncio.create_task(reloader.reload_now())
                )
            except (NotImplementedError, RuntimeError, ValueError) as exc:
                logger.debug("SIGHUP reload trigger unavailable: %s", exc)
        try:
            if config.start_on_launch:
                await manager.rotate()
            await proxy.start()
            yield
        finally:
            try:
                if reloader is not None:
                    loop.remove_signal_handler(signal.SIGHUP)
                if poller is not None:
                    stop.set()
                    await poller
            finally:
                try:
                    await proxy.close()
                finally:
                    await manager.close()

    app = FastAPI(title="Tunnel Switch", lifespan=lifespan)
    app.state.manager = manager
    app.state.http_proxy = proxy
    app.state.config = config
    app.state.loop = None
    # Every reload swaps the config attribute of these objects, so the API, the
    # tunnel manager and the HTTP bridge switch configuration as one reference.
    app.state.reloader = (
        None
        if config_path is None
        else ConfigReloader(
            config_path,
            config,
            targets=[manager, proxy, app.state],
            hooks=on_reload,
            prepare=prepare,
            busy=lambda: manager.busy,
        )
    )

    def authorize(authorization: str | None = Header(default=None)):
        # The current token is read per request so a reload can rotate it.
        current = app.state.config.api.token or token
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), f"Bearer {current}".encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    def reload_block():
        reloader = app.state.reloader
        return {
            "watching": reloader is not None and watch,
            "path": str(config_path) if config_path is not None else None,
            "last_reload": reloader.last_reload if reloader else None,
            "last_error": reloader.last_error if reloader else None,
            "deferred": list(reloader.deferred) if reloader else [],
        }

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
            "config_reload": reload_block(),
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
        if reloader is not None:
            # A rotation creates a fresh ssh process, so a deferred SOCKS bind
            # becomes live here instead of waiting for a restart.
            reloader.apply_pending_binds()
        try:
            return await manager.rotate()
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

    return app
