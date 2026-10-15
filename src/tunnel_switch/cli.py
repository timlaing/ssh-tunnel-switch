from pathlib import Path
from typing import Annotated
import httpx
import typer
import uvicorn
from .api import create_app
from .config import ensure_api_token, load_config

app = typer.Typer(no_args_is_help=True, help="Manage the SSH SOCKS tunnel rotation API.")
ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="Required YAML configuration file.")]


def settings(path: Path):
    config = load_config(path)
    token = config.api.token
    if not token:
        raise ValueError("API token is missing from YAML; start the service with serve to generate and save it")
    return config, token


@app.command()
def validate(config: ConfigOption):
    """Validate YAML and SSH identity paths."""
    load_config(config)
    typer.echo("Configuration valid")


@app.command()
def serve(config: ConfigOption):
    """Start the API and manage one SSH process; Ctrl-C stops the tunnel."""
    cfg = load_config(config)
    token = ensure_api_token(config, cfg)
    typer.echo(f"API token: {token}")
    if cfg.http_proxy.enabled:
        ip = str(cfg.http_proxy.ip)
        host = f"[{ip}]" if ":" in ip else ip
        typer.echo(f"HTTP proxy: http://{host}:{cfg.http_proxy.port}")
    uvicorn.run(create_app(cfg, token), host=str(cfg.api.ip), port=cfg.api.port, workers=1)


def request(path: Path, method: str, endpoint: str):
    cfg, token = settings(path)
    ip = str(cfg.api.ip)
    ip = "127.0.0.1" if ip == "0.0.0.0" else "::1" if ip == "::" else ip
    host = f"[{ip}]" if ":" in ip else ip
    response = httpx.request(method, f"http://{host}:{cfg.api.port}/{endpoint}",
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=cfg.startup_timeout_seconds + cfg.stop_timeout_seconds + 5,
                             trust_env=False)
    response.raise_for_status()
    typer.echo(response.text)


@app.command()
def status(config: ConfigOption):
    """Display connection ID, start time and current tunnel state."""
    request(config, "GET", "status")


@app.command()
def rotate(config: ConfigOption):
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
