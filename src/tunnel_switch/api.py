from contextlib import asynccontextmanager
import secrets
from fastapi import Depends, FastAPI, Header, HTTPException
from .config import Config
from .http_proxy import HTTPProxyServer
from .tunnel import RotationRejected, TunnelFailure, TunnelManager


def create_app(config: Config, token: str, manager: TunnelManager | None = None) -> FastAPI:
    if not token:
        raise ValueError("API token must not be empty")
    manager = manager or TunnelManager(config)
    proxy = HTTPProxyServer(config)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if config.start_on_launch:
                await manager.rotate()
            await proxy.start()
            yield
        finally:
            try:
                await proxy.close()
            finally:
                await manager.close()

    app = FastAPI(title="Tunnel Switch", lifespan=lifespan)
    app.state.manager = manager
    app.state.http_proxy = proxy

    async def authorize(authorization: str | None = Header(default=None)):
        if authorization is None or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    @app.get("/status", dependencies=[Depends(authorize)])
    async def status():
        return {**manager.status(), "http_proxy": {
            "enabled": config.http_proxy.enabled, "ip": str(config.http_proxy.ip),
            "port": config.http_proxy.port}}

    @app.post("/rotate", dependencies=[Depends(authorize)])
    async def rotate():
        try:
            return await manager.rotate()
        except RotationRejected as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
        except TunnelFailure as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app
