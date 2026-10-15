from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
import yaml
import os
import secrets
import tempfile
from ruamel.yaml import YAML
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Server(StrictModel):
    name: str | None = Field(default=None, min_length=1)
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.:%_-]*$")
    user: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
    port: int | None = Field(default=None, ge=1, le=65535)
    identity_file: Path | None = None

    @model_validator(mode="after")
    def default_name(self):
        if self.name is None:
            self.name = self.host
        return self


class Proxy(StrictModel):
    ip: IPv4Address | IPv6Address = IPv4Address("127.0.0.1")
    port: int = Field(default=1080, ge=1, le=65535)


class HTTPProxy(StrictModel):
    enabled: bool = True
    ip: IPv4Address | IPv6Address = IPv4Address("127.0.0.1")
    port: int = Field(default=8118, ge=1, le=65535)
    connect_timeout_seconds: float = Field(default=10, gt=0, le=120)
    idle_timeout_seconds: float = Field(default=120, gt=0)
    header_limit_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_connections: int = Field(default=128, ge=1)


class API(StrictModel):
    ip: IPv4Address | IPv6Address = IPv4Address("127.0.0.1")
    port: int = Field(default=8765, ge=1, le=65535)
    token: str | None = Field(default=None, repr=False)

    @field_validator("token", mode="before")
    @classmethod
    def empty_token(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class Config(StrictModel):
    servers: list[Server] = Field(min_length=1)
    proxy: Proxy
    api: API = Field(default_factory=API)
    http_proxy: HTTPProxy = Field(default_factory=HTTPProxy)
    cooldown_seconds: float = Field(default=30, ge=0)
    startup_timeout_seconds: float = Field(default=15, gt=0, le=120)
    stop_timeout_seconds: float = Field(default=5, gt=0, le=60)
    start_on_launch: bool = True

    @model_validator(mode="after")
    def unique_names(self):
        if len({server.name for server in self.servers}) != len(self.servers):
            raise ValueError("SSH server names must be unique")
        listeners = [self.proxy, self.api]
        if self.http_proxy.enabled:
            listeners.append(self.http_proxy)
        for index, left in enumerate(listeners):
            for right in listeners[index + 1:]:
                if left.port == right.port and (left.ip == right.ip or
                        (left.ip.version == right.ip.version and
                         (left.ip.is_unspecified or right.ip.is_unspecified))):
                    raise ValueError("API, HTTP proxy and SOCKS proxy must use different sockets")
        return self


def load_config(path: Path) -> Config:
    config = Config.model_validate(yaml.safe_load(path.read_text()))
    for server in config.servers:
        if server.identity_file is not None:
            identity = server.identity_file.expanduser()
            server.identity_file = identity if identity.is_absolute() else path.resolve().parent / identity
            if not server.identity_file.is_file():
                raise ValueError(f"Identity file does not exist: {server.identity_file}")
    return config


def ensure_api_token(path: Path, config: Config) -> str:
    """Persist a generated token atomically, retaining YAML comments and values."""
    if config.api.token:
        return config.api.token
    target = path.resolve()
    yaml_editor = YAML()
    yaml_editor.preserve_quotes = True
    document = yaml_editor.load(target.read_text())
    if document.get("api") is None:
        document["api"] = {}
    token = secrets.token_urlsafe(32)
    document["api"]["token"] = token
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            yaml_editor.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # The temporary file is mode 0600; replacement keeps the saved token private.
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    config.api.token = token
    return token
