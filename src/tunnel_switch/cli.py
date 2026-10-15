from pathlib import Path
from typing import Annotated
import httpx
import typer
import uvicorn
from .api import create_app
from .config import ensure_api_token, load_config, resolve_state_file

app = typer.Typer(no_args_is_help=True, help="Manage the SSH SOCKS tunnel rotation API.")
ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="YAML file; defaults to ~/.config/ssh-tunnel-switch/config.yaml, then ./config.yaml.")]


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
        raise ValueError("API token is missing from YAML; start the service with serve to generate and save it")
    return config, token


@app.command("init-config")
def init_config(output: Annotated[Path, typer.Option("--output", "-o", help="New configuration file; existing files are never overwritten.")] = Path("config.yaml")):
    """Write a private example configuration, then edit its SSH host aliases."""
    from importlib.resources import files
    import os
    output = output.expanduser()
    content = files("tunnel_switch").joinpath("example.yaml").read_text(encoding="utf-8")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    typer.echo(f"Created {output}; edit SSH host aliases before starting the service.")


@app.command()
def validate(config: ConfigOption = None):
    """Validate YAML and SSH identity paths."""
    load_config(resolve_config(config))
    typer.echo("Configuration valid")


@app.command()
def serve(
    config: ConfigOption = None,
    state_file: Annotated[Path | None, typer.Option(
        "--state-file", help="Override the last-connected-server state file (relative paths use the current directory).")] = None,
    server_name: Annotated[str | None, typer.Option(
        "--server-name", help="Start at this configured server name instead of the saved server.")] = None,
):
    """Start the API and manage one SSH process; Ctrl-C stops the tunnel."""
    config = resolve_config(config)
    cfg = load_config(config)
    if state_file is not None:
        cfg.state_file = resolve_state_file(state_file, cfg, config)
    if server_name is not None:
        if server_name not in {server.name for server in cfg.servers}:
            raise ValueError(f"Unknown server name: {server_name}. Choose one of: {', '.join(server.name for server in cfg.servers)}")
        cfg.server_name = server_name
    token = ensure_api_token(config, cfg)
    typer.echo(f"API token: {token}")
    if cfg.state_file is not None:
        typer.echo(f"State file: {cfg.state_file}")
    if cfg.http_proxy.enabled:
        ip = str(cfg.http_proxy.ip)
        host = f"[{ip}]" if ":" in ip else ip
        typer.echo(f"HTTP proxy: http://{host}:{cfg.http_proxy.port}")
    uvicorn.run(create_app(cfg, token), host=str(cfg.api.ip), port=cfg.api.port, workers=1)


def request(path: Path | None, method: str, endpoint: str):
    cfg, token = settings(path)
    ip = str(cfg.api.ip)
    if ip == "0.0.0.0":
        ip = "127.0.0.1"
    elif ip == "::":
        ip = "::1"
    host = f"[{ip}]" if ":" in ip else ip
    response = httpx.request(method, f"http://{host}:{cfg.api.port}/{endpoint}",
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=cfg.startup_timeout_seconds + cfg.stop_timeout_seconds + 5,
                             trust_env=False)
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
