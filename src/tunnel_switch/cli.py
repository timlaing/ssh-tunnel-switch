"""Provide tunnel service commands and interactive status-panel actions."""

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from contextlib import redirect_stdout
from datetime import UTC, datetime
from importlib.resources import files
from ipaddress import IPv4Address
from pathlib import Path
from threading import Event, Thread
from typing import Annotated

import click
import httpx
import typer
import uvicorn
from fastapi import FastAPI

from . import __version__
from .api import create_app
from .config import Config, ensure_api_token, load_config, resolve_state_file
from .logs import configure_logging
from .reload import ReloadOutcome
from .status import REFRESH_SECONDS, PanelState, run_status_display
from .tunnel import RotationRejected, TunnelManager

logger = logging.getLogger(__name__)

app = typer.Typer(
    no_args_is_help=True,
    help="Manage the SSH SOCKS tunnel rotation API.",
)


def _version_callback(*, value: bool) -> None:
    if value:
        typer.echo(f"tunnel-switch {__version__}")
        raise typer.Exit


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version of this tunnel-switch build and exit.",
    ),
) -> None:
    """Manage the SSH SOCKS tunnel rotation API."""


def is_tty() -> bool:
    """Check whether stdout supports the interactive live status display."""
    return sys.stdout.isatty()


ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help=(
            "YAML file; defaults to ~/.config/ssh-tunnel-switch/config.yaml, "
            "then ./config.yaml."
        ),
    ),
]


def resolve_config(path: Path | None) -> Path:
    """Expand an explicit path or choose the user config before the local file."""
    if path is not None:
        return path.expanduser()
    preferred = Path("~/.config/ssh-tunnel-switch/config.yaml").expanduser()
    return preferred if preferred.exists() else Path("config.yaml")


def settings(path: Path | None) -> tuple[Config, str]:
    """Load validated client settings and require a saved API bearer token."""
    path = resolve_config(path)
    config = load_config(path)
    token = config.api.token
    if not token:
        message = (
            "API token is missing from YAML; "
            "start the service with serve to generate and save it"
        )
        raise ValueError(message)
    return config, token


@app.command("init-config")
def init_config(
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="New configuration file; existing files are never overwritten.",
        ),
    ] = Path("config.yaml"),
) -> None:
    """Write a private example configuration, then edit its SSH host aliases."""
    output = output.expanduser()
    content = (
        files("tunnel_switch").joinpath("example.yaml").read_text(encoding="utf-8")
    )
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    typer.echo(f"Created {output}; edit SSH host aliases before starting the service.")


@app.command()
def validate(config: ConfigOption = None) -> None:
    """Validate YAML and SSH identity paths."""
    load_config(resolve_config(config))
    typer.echo("Configuration valid")


def _submit_action[**P](
    application: FastAPI,
    panel_state: PanelState,
    name: str,
    run: Callable[P, Coroutine[object, object, str]],
) -> Callable[P, None]:
    """Schedule a panel action on the API loop and report its future's result."""

    def dispatch(*args: P.args, **kwargs: P.kwargs) -> None:
        loop = getattr(application.state, "loop", None)
        if loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(run(*args, **kwargs), loop)

        def done(completed: Future[str]) -> None:
            try:
                panel_state.last_action = completed.result()
            except RotationRejected as exc:
                panel_state.last_action = f"{name}: {exc} (retry in {exc.retry_after}s)"
            except Exception as exc:
                # This callback is the UI error boundary, including failures
                # from optional reload hooks; keep the display thread alive.
                logger.exception("Status-panel action %s failed", name)
                panel_state.last_action = f"{name}: {exc}"

        future.add_done_callback(done)

    return dispatch


def key_actions(
    application: FastAPI,
    manager: TunnelManager,
    panel_state: PanelState,
) -> dict[str, Callable[..., None]]:
    """Build the display-thread callbacks that run coroutines on the API loop.

    The display thread cannot await, so each key schedules its coroutine on the
    service event loop and records the outcome (or the rejection, such as an
    active cooldown) as the panel's last action. Actions take arguments when
    the key resolves a choice: the server picker passes the server to connect
    to.
    """

    def pending_bind() -> Callable[[], Config | None] | None:
        """Return the deferred-bind hook to run only after rotation is admitted."""
        reloader = getattr(application.state, "reloader", None)
        return None if reloader is None else reloader.apply_pending_binds

    async def reload_config() -> str:
        reloader = getattr(application.state, "reloader", None)
        if reloader is None:
            return "reload: no configuration file was given to the service"
        return f"reload: {(await reloader.reload_now()).summary()}"

    async def connect() -> str:
        status = manager.status()
        if status["state"] == "connected":
            return f"already connected to {status['server']}; Alt+R restarts it"
        await manager.restart(pending_bind())
        return f"connect: tunnel up on {manager.status()['server']}"

    async def restart() -> str:
        await manager.restart(pending_bind())
        return f"restart: {manager.status()['server']}"

    async def rotate() -> str:
        # A key press is a deliberate request from the operator watching the
        # panel, so the cooldown backoff gates the API, not this key.
        await manager.rotate(pending_bind(), ignore_cooldown=True)
        return f"rotate: {manager.status()['server']}"

    async def select(server_name: str) -> str:
        # The picker already asked for one server by name: no failover, no
        # cooldown — it comes up or its failure is reported as-is.
        await manager.select_server(server_name, pending_bind())
        return f"select: {manager.status()['server']}"

    async def disconnect() -> str:
        await manager.disconnect()
        return "disconnect: tunnel stopped"

    return {
        "reload": _submit_action(application, panel_state, "reload", reload_config),
        "connect": _submit_action(application, panel_state, "connect", connect),
        "disconnect": _submit_action(
            application,
            panel_state,
            "disconnect",
            disconnect,
        ),
        "restart": _submit_action(application, panel_state, "restart", restart),
        "rotate": _submit_action(application, panel_state, "rotate", rotate),
        "select": _submit_action(application, panel_state, "select", select),
    }


@app.command()
def serve(
    config: ConfigOption = None,
    state_file: Annotated[
        Path | None,
        typer.Option(
            "--state-file",
            help=(
                "Override the last-connected-server state file "
                "(relative paths use the current directory)."
            ),
        ),
    ] = None,
    server_name: Annotated[
        str | None,
        typer.Option(
            "--server-name",
            help="Start at this configured server name instead of the saved server.",
        ),
    ] = None,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch/--no-watch",
            help=(
                "Poll the YAML file and apply configuration changes without restarting."
            ),
        ),
    ] = True,
) -> None:
    """Start the API and manage one SSH process; Ctrl-C stops the service."""
    config = resolve_config(config)
    cfg = load_config(config)
    _apply_startup_overrides(cfg, config, state_file, server_name)
    token = ensure_api_token(config, cfg)
    # An interactive terminal keeps a WARNING+ console handler so failures that
    # happen before (or behind) the status panel are still visible, even when
    # file logging is disabled; headless runs get every record on stderr.
    log_file = configure_logging(
        cfg.logging,
        console=True,
        console_level=logging.WARNING if is_tty() else logging.INFO,
    )
    _echo_startup(cfg, token, log_file)
    manager = TunnelManager(cfg)
    panel_state = PanelState()
    application = create_app(
        cfg,
        token,
        manager,
        config_path=config,
        watch=watch,
        on_reload=[_reload_feedback(panel_state)],
        prepare=_cli_overrides(cfg, state_file, server_name),
    )
    if is_tty():
        # uvicorn keeps the main thread so Ctrl-C and SIGTERM still trigger its
        # graceful shutdown (and the SSH process cleanup in the app lifespan);
        # the status display owns the console from a daemon thread instead.
        _display_and_serve(application, cfg, manager, panel_state)
    else:
        _run_uvicorn(application, cfg)


def _apply_startup_overrides(
    cfg: Config,
    config: Path,
    state_file: Path | None,
    server_name: str | None,
) -> None:
    if state_file is not None:
        cfg.state_file = resolve_state_file(state_file, cfg, config)
    if server_name is None:
        return
    if server_name not in {server.name for server in cfg.servers}:
        message = (
            f"Unknown server name: {server_name}. Choose one of: "
            f"{', '.join(server.name for server in cfg.servers)}"
        )
        raise ValueError(message)
    cfg.server_name = server_name


def _echo_startup(cfg: Config, token: str, log_file: Path | None) -> None:
    typer.echo(f"Version: {__version__}")
    typer.echo(f"API token: {token}")
    if cfg.state_file is not None:
        typer.echo(f"State file: {cfg.state_file}")
    if cfg.http_proxy.enabled:
        ip = str(cfg.http_proxy.ip)
        host = f"[{ip}]" if ":" in ip else ip
        typer.echo(f"HTTP proxy: http://{host}:{cfg.http_proxy.port}")
    if log_file is not None:
        typer.echo(f"Log file: {log_file}")


def _console_level() -> int:
    return logging.WARNING if is_tty() else logging.INFO


def _reload_feedback(panel_state: PanelState) -> Callable[[ReloadOutcome], None]:
    """Feed the panel and reconfigure logging after every reload attempt."""

    def reloaded(outcome: ReloadOutcome) -> None:
        panel_state.last_reload = f"{datetime.now(UTC):%H:%M:%S} {outcome.summary()}"
        if outcome.applied and outcome.config is not None:
            configure_logging(
                outcome.config.logging,
                console=True,
                console_level=_console_level(),
            )

    return reloaded


def _cli_overrides(
    cfg: Config,
    state_file: Path | None,
    server_name: str | None,
) -> Callable[[Config], None]:
    """Keep CLI overrides across reloads until the service restarts."""

    def prepare(desired: Config) -> None:
        if state_file is not None:
            desired.state_file = cfg.state_file
        if server_name is not None:
            desired.server_name = server_name

    return prepare


def _run_uvicorn(application: FastAPI, cfg: Config) -> None:
    uvicorn.run(
        application,
        host=str(cfg.api.ip),
        port=cfg.api.port,
        workers=1,
        log_config=None,
    )


def _display_and_serve(
    application: FastAPI,
    cfg: Config,
    manager: TunnelManager,
    panel_state: PanelState,
) -> None:
    stop = Event()
    display = Thread(
        target=run_status_display,
        args=(manager, stop),
        kwargs={
            "actions": key_actions(application, manager, panel_state),
            "state": panel_state,
        },
        daemon=True,
    )
    display.start()
    try:
        _run_uvicorn(application, cfg)
    finally:
        stop.set()
        display.join(timeout=2 * REFRESH_SECONDS + 1)


def request(path: Path | None, method: str, endpoint: str) -> None:
    """Call an authenticated local API endpoint and print its response body."""
    cfg, token = settings(path)
    ip = str(cfg.api.ip)
    if isinstance(cfg.api.ip, IPv4Address) and cfg.api.ip.is_unspecified:
        ip = "127.0.0.1"
    elif ip == "::":
        ip = "::1"
    host = f"[{ip}]" if ":" in ip else ip
    response = httpx.request(
        method,
        # Local loopback REST API served by this tool without TLS by design.
        f"http://{host}:{cfg.api.port}/{endpoint}",  # NOSONAR S5332: loopback only
        headers={"Authorization": f"Bearer {token}"},
        timeout=cfg.startup_timeout_seconds + cfg.stop_timeout_seconds + 5,
        trust_env=False,
    )
    response.raise_for_status()
    typer.echo(response.text)


@app.command()
def status(config: ConfigOption = None) -> None:
    """Display connection ID, start time and current tunnel state."""
    request(config, "GET", "status")


@app.command()
def rotate(config: ConfigOption = None) -> None:
    """Stop the current tunnel and start the next configured SSH server."""
    request(config, "POST", "rotate")


def main() -> None:
    """Run the CLI and report command failures with usage guidance."""
    try:
        app(standalone_mode=False)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        ctx = click.Context(typer.main.get_command(app))
        with redirect_stdout(sys.stderr):
            typer.echo(ctx.get_help(), err=True)
        typer.echo("Use tunnel-switch COMMAND --help for command options.", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
