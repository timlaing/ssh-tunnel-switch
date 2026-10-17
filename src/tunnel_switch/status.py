"""Live terminal status display for an interactive ``serve`` run."""

from datetime import datetime, timezone
from threading import Event

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .config import Config

REFRESH_SECONDS = 0.5
STATE_STYLES = {"connected": "green", "rotating": "yellow", "disconnected": "red"}


def _endpoint(ip, port: int) -> str:
    address = str(ip)
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}"


def _elapsed(started_at: str | None) -> str:
    if not started_at:
        return ""
    since = datetime.fromisoformat(started_at)
    seconds = int((datetime.now(timezone.utc) - since).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def status_panel(config: Config, status: dict, log_file=None) -> Panel:
    """Render the current tunnel state as a single status panel."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="left")
    table.add_column(justify="left")
    state = status["state"]
    table.add_row("State", f"[{STATE_STYLES.get(state, 'white')}]{state}[/]")
    server = status["server"] or "-"
    table.add_row("Server", server)
    connection = (status["connection_id"] or "")[:8]
    table.add_row(
        "Connection", f"{connection} ({status['server']})" if connection else "-"
    )
    started = status["started_at"] or "-"
    if status["started_at"]:
        started += f" (up {_elapsed(status['started_at'])})"
    table.add_row("Started", started)
    proxy = status["proxy"]
    table.add_row("SOCKS proxy", _endpoint(proxy["ip"], proxy["port"]))
    if config.http_proxy.enabled:
        table.add_row(
            "HTTP proxy",
            _endpoint(config.http_proxy.ip, config.http_proxy.port),
        )
    table.add_row("API", _endpoint(config.api.ip, config.api.port))
    table.add_row("Log file", str(log_file) if log_file else "disabled")
    if status["last_error"]:
        table.add_row("Last error", f"[red]{status['last_error']}[/]")
    return Panel(Group(table), title="SSH Tunnel Switch", border_style="blue")


def run_status_display(
    config: Config, manager, log_file, stop: Event, console: Console | None = None
):
    """Refresh the status panel until ``stop`` is set.

    Runs inside a daemon thread so the main thread keeps uvicorn's normal
    signal handling; every console write happens in this one thread.
    """
    console = console or Console()
    with Live(
        status_panel(config, manager.status(), log_file),
        console=console,
        refresh_per_second=2,
        transient=True,
    ) as live:
        while not stop.wait(REFRESH_SECONDS):
            live.update(status_panel(config, manager.status(), log_file))
