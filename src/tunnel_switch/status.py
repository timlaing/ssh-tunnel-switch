"""Live terminal status display for an interactive ``serve`` run.

The display owns one daemon thread: it renders the panel and, when stdin is a
terminal, reads single key presses (reload, connect, disconnect, restart,
rotate). Keys run as coroutines on the service event loop; the terminal is put
in cbreak mode so single keys arrive without echoing while ``ISIG`` stays
enabled, keeping Ctrl-C a normal SIGINT for uvicorn's shutdown.
"""

from datetime import datetime, timezone
import os
import select
from threading import Event

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Config

REFRESH_SECONDS = 0.5
ESCAPE_WAIT_SECONDS = 0.05
STATE_STYLES = {"connected": "green", "rotating": "yellow", "disconnected": "red"}
KEYS = {b"\x12": "reload", b"\x05": "connect", b"\x04": "disconnect", b"\x0e": "rotate"}
FOOTER = (
    "Ctrl+R: reload config · Alt+R: restart tunnel",
    "Ctrl+N: rotate server · Ctrl+E: connect · Ctrl+D: disconnect · Ctrl+C: quit",
)


def decode_key(sequence: bytes) -> str | None:
    """Map one terminal read to an action, ignoring every other byte."""
    escape = sequence.find(b"\x1b")
    if escape != -1 and sequence[escape + 1 : escape + 2] == b"r":
        return "restart"
    for key, action in KEYS.items():
        if key in sequence:
            return action
    return None


class PanelState:
    """Feedback text shared between the key handler and the display thread."""

    def __init__(self):
        self.last_reload = None
        self.last_action = None


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


def status_panel(
    config: Config,
    status: dict,
    state: PanelState | None = None,
    keys: bool = False,
) -> Panel:
    """Render the current tunnel state as a single status panel."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="left")
    table.add_column(justify="left")
    state_name = status["state"]
    table.add_row("State", f"[{STATE_STYLES.get(state_name, 'white')}]{state_name}[/]")
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
    table.add_row(
        "Log file", str(config.logging.file) if config.logging.file else "disabled"
    )
    if state is not None and state.last_reload:
        table.add_row("Last reload", state.last_reload)
    if state is not None and state.last_action:
        table.add_row("Last action", state.last_action)
    if status["last_error"]:
        table.add_row("Last error", f"[red]{status['last_error']}[/]")
    body = [table]
    if keys:
        body.append(Text(FOOTER[0], style="dim"))
        body.append(Text(FOOTER[1], style="dim"))
    return Panel(
        Group(*body),
        title=f"SSH Tunnel Switch {__version__}",
        border_style="blue",
    )


def _key_reader():
    """Put stdin in cbreak mode, returning its descriptor and a restore call.

    cbreak disables echo and line buffering so single keys are readable, while
    ``ISIG`` is left enabled: Ctrl-C still raises SIGINT and uvicorn still
    performs its graceful shutdown. Non-terminal and non-POSIX runs simply get
    no key handling.
    """
    if os.name != "posix":
        return None, lambda: None
    import sys
    import termios
    import tty

    stream = sys.stdin
    try:
        if not stream.isatty():
            return None, lambda: None
        descriptor = stream.fileno()
        saved = termios.tcgetattr(descriptor)
        tty.setcbreak(descriptor)
    except (OSError, termios.error):
        return None, lambda: None

    def restore():
        try:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass

    return descriptor, restore


def _handle_keys(descriptor: int, actions, state: PanelState) -> None:
    sequence = os.read(descriptor, 32)
    # Alt+R may arrive as ESC and "r" in separate reads.
    if (
        sequence.endswith(b"\x1b")
        and select.select([descriptor], [], [], ESCAPE_WAIT_SECONDS)[0]
    ):
        sequence += os.read(descriptor, 8)
    action = decode_key(sequence)
    if action is None or actions is None or action not in actions:
        return
    try:
        actions[action]()
    except OSError as exc:
        state.last_action = f"{action}: {exc}"


def run_status_display(
    manager,
    stop: Event,
    console: Console | None = None,
    actions=None,
    state: PanelState | None = None,
):
    """Refresh the status panel, and handle keys, until ``stop`` is set.

    Runs inside a daemon thread so the main thread keeps uvicorn's normal
    signal handling; every console write happens in this one thread, and the
    configuration read is always the manager's current one.
    """
    console = console or Console()
    state = state or PanelState()
    descriptor, restore = _key_reader()
    keys = descriptor is not None and actions is not None
    try:
        with Live(
            status_panel(manager.config, manager.status(), state, keys=keys),
            console=console,
            refresh_per_second=2,
            transient=True,
        ) as live:
            while True:
                if descriptor is not None:
                    readable, _, _ = select.select(
                        [descriptor], [], [], REFRESH_SECONDS
                    )
                else:
                    readable = []
                    if stop.wait(REFRESH_SECONDS):
                        break
                live.update(
                    status_panel(manager.config, manager.status(), state, keys=keys)
                )
                if stop.is_set():
                    break
                if readable:
                    _handle_keys(descriptor, actions, state)
    finally:
        restore()
