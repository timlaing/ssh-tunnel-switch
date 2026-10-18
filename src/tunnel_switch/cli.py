import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Annotated
import httpx
import typer
import uvicorn
from .api import create_app
from .config import ensure_api_token, load_config, resolve_state_file
from .logs import configure_logging
from .status import PanelState, REFRESH_SECONDS, run_status_display
from .tunnel import RotationRejected, TunnelManager

app = typer.Typer(
    no_args_is_help=True, help="Manage the SSH SOCKS tunnel rotation API."
)


def is_tty() -> bool:
    """True when stdout is a terminal, enabling the live status display."""
    import sys

    return sys.stdout.isatty()


ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="YAML file; defaults to ~/.config/ssh-tunnel-switch/config.yaml, then ./config.yaml.",
    ),
]


def resolve_config(path: Path | None) -> Path:
    if path is not None:
        return path.expanduser()
    preferred = Path("~/.config/ssh-tunnel-switch/config.yaml").expanduser()
    return preferred if preferred.exists() else Path("config.yaml")


def settings(path: Path | None):
    path = resolve_config(path)
    config = load_config(path)
    token = config.api.token
    if not token:
        raise ValueError(
            "API token is missing from YAML; start the service with serve to generate and save it"
        )
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
):
    """Write a private example configuration, then edit its SSH host aliases."""
    from importlib.resources import files
    import os

    output = output.expanduser()
    content = (
        files("tunnel_switch").joinpath("example.yaml").read_text(encoding="utf-8")
    )
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    typer.echo(f"Created {output}; edit SSH host aliases before starting the service.")


@app.command()
def validate(config: ConfigOption = None):
    """Validate YAML and SSH identity paths."""
    load_config(resolve_config(config))
    typer.echo("Configuration valid")


def key_actions(application, manager, panel_state):
    """Build the display-thread callbacks that run coroutines on the API loop.

    The display thread cannot await, so each key schedules its coroutine on the
    service event loop and records the outcome (or the rejection, such as an
    active cooldown) as the panel's last action.
    """

    def submit(name, run):
        def dispatch():
            loop = getattr(application.state, "loop", None)
            if loop is None or loop.is_closed():
                return
            future = asyncio.run_coroutine_threadsafe(run(), loop)

            def done(completed):
                try:
                    panel_state.last_action = completed.result()
                except RotationRejected as exc:
                    panel_state.last_action = (
                        f"{name}: {exc} (retry in {exc.retry_after}s)"
                    )
                except Exception as exc:
                    panel_state.last_action = f"{name}: {exc}"

            future.add_done_callback(done)

        return dispatch

    def apply_pending():
        reloader = getattr(application.state, "reloader", None)
        if reloader is not None:
            # A new ssh process rebinds the SOCKS socket, so a deferred bind
            # becomes live on every connection change.
            reloader.apply_pending_binds()

    async def reload_config():
        reloader = getattr(application.state, "reloader", None)
        if reloader is None:
            return "reload: no configuration file was given to the service"
        return f"reload: {(await reloader.reload_now()).summary()}"

    async def connect():
        status = manager.status()
        if status["state"] == "connected":
            return f"already connected to {status['server']}; Alt+R restarts it"
        apply_pending()
        await manager.restart()
        return f"connect: tunnel up on {manager.status()['server']}"

    async def restart():
        apply_pending()
        await manager.restart()
        return f"restart: {manager.status()['server']}"

    async def rotate():
        apply_pending()
        await manager.rotate()
        return f"rotate: {manager.status()['server']}"

    async def disconnect():
        await manager.disconnect()
        return "disconnect: tunnel stopped"

    return {
        "reload": submit("reload", reload_config),
        "connect": submit("connect", connect),
        "disconnect": submit("disconnect", disconnect),
        "restart": submit("restart", restart),
        "rotate": submit("rotate", rotate),
    }


@app.command()
def serve(
    config: ConfigOption = None,
    state_file: Annotated[
        Path | None,
        typer.Option(
            "--state-file",
            help="Override the last-connected-server state file (relative paths use the current directory).",
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
            help="Poll the YAML file and apply configuration changes without restarting.",
        ),
    ] = True,
):
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


def _apply_startup_overrides(cfg, config, state_file, server_name):
    if state_file is not None:
        cfg.state_file = resolve_state_file(state_file, cfg, config)
    if server_name is None:
        return
    if server_name not in {server.name for server in cfg.servers}:
        raise ValueError(
            f"Unknown server name: {server_name}. Choose one of: {', '.join(server.name for server in cfg.servers)}"
        )
    cfg.server_name = server_name


def _echo_startup(cfg, token, log_file):
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


def _reload_feedback(panel_state):
    """Feed the panel and reconfigure logging after every reload attempt."""

    def reloaded(outcome):
        panel_state.last_reload = (
            f"{datetime.now(timezone.utc):%H:%M:%S} {outcome.summary()}"
        )
        if outcome.applied and outcome.config is not None:
            configure_logging(
                outcome.config.logging,
                console=True,
                console_level=_console_level(),
            )

    return reloaded


def _cli_overrides(cfg, state_file, server_name):
    """CLI overrides outlive a reload; the YAML values apply after a restart."""

    def prepare(desired):
        if state_file is not None:
            desired.state_file = cfg.state_file
        if server_name is not None:
            desired.server_name = server_name

    return prepare


def _run_uvicorn(application, cfg):
    uvicorn.run(
        application,
        host=str(cfg.api.ip),
        port=cfg.api.port,
        workers=1,
        log_config=None,
    )


def _display_and_serve(application, cfg, manager, panel_state):
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


def request(path: Path | None, method: str, endpoint: str):
    cfg, token = settings(path)
    ip = str(cfg.api.ip)
    if ip == "0.0.0.0":
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
def status(config: ConfigOption = None):
    """Display connection ID, start time and current tunnel state."""
    request(config, "GET", "status")


@app.command()
def rotate(config: ConfigOption = None):
    """Stop the current tunnel and start the next configured SSH server."""
    request(config, "POST", "rotate")


def main():
    try:
        app(standalone_mode=False)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        import click

        ctx = click.Context(typer.main.get_command(app))
        from contextlib import redirect_stdout
        import sys

        with redirect_stdout(sys.stderr):
            typer.echo(ctx.get_help(), err=True)
        typer.echo("Use tunnel-switch COMMAND --help for command options.", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
