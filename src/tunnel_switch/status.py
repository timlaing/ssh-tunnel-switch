"""Live terminal status display for an interactive ``serve`` run.

The display owns one daemon thread: it renders the panel and, when stdin is a
terminal, reads single key presses (reload, connect, disconnect, restart,
rotate, select server). Keys run as coroutines on the service event loop; the
terminal is put in cbreak mode so single keys arrive without echoing while
``ISIG`` stays enabled, keeping Ctrl-C a normal SIGINT for uvicorn's shutdown.

The picker has two openers: Alt+S, for a terminal whose Option key sends
Escape, and Ctrl+S, which needs no modifier setting at all. cbreak alone would
swallow Ctrl+S as XOFF, so software flow control is switched off for as long as
the panel is up.

The select key opens an inline picker instead of dispatching at once: the
panel lists every configured server, arrow keys (or ``j``/``k``) move the
highlight, Enter hands the highlighted server to the select action and Esc
closes the picker without changing anything.
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
KEYS = {
    b"\x12": "reload",
    b"\x05": "connect",
    b"\x04": "disconnect",
    b"\x0e": "rotate",
    b"\x1bs": "select",
    b"\x13": "select",
}
FOOTER = (
    "Ctrl+R: reload config · Alt+R: restart tunnel · Alt+S/Ctrl+S: select server",
    "Ctrl+N: rotate server · Ctrl+E: connect · Ctrl+D: disconnect · Ctrl+C: quit",
)
PICKER_FOOTER = (
    "↑/↓ or j/k: highlight · Enter: connect to the highlighted server · Esc: cancel",
)
# Picker navigation, matched as one complete read: arrows and j/k move the
# highlight, Enter and Esc resolve it. Every other byte is ignored.
PICKER_KEYS = {b"\x1b[A": -1, b"k": -1, b"\x1b[B": 1, b"j": 1}


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
        # Highlighted row of the open server picker; None while it is closed.
        self.picker_index = None


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


def _server_picker(config: Config, status: dict, state: PanelState) -> Group:
    """List the configured servers, marking the highlighted and current one."""
    current = status["server"]
    lines = [Text("Select server", style="bold")]
    for index, server in enumerate(config.servers):
        picked = index == state.picker_index
        host = "" if server.host == server.name else f" ({server.host})"
        marker = "❯ " if picked else "  "
        note = " · current" if server.name == current else ""
        lines.append(
            Text(
                f"{marker}{server.name}{host}{note}",
                style="bold" if picked else "dim",
            )
        )
    return Group(*lines)


def _summary_rows(config: Config, status: dict) -> list[tuple[str, str]]:
    """The fixed rows: tunnel identity, listeners and the log file."""
    state_name = status["state"]
    connection = (status["connection_id"] or "")[:8]
    started = status["started_at"] or "-"
    if status["started_at"]:
        started += f" (up {_elapsed(status['started_at'])})"
    rows = [
        ("State", f"[{STATE_STYLES.get(state_name, 'white')}]{state_name}[/]"),
        ("Server", status["server"] or "-"),
        ("Connection", f"{connection} ({status['server']})" if connection else "-"),
        ("Started", started),
    ]
    # The counter only exists in a live status dict: a caller that builds one
    # by hand (a test, an older client) simply gets no request row.
    requests = status.get("requests")
    if requests is not None:
        rows.append(
            (
                "HTTP requests",
                f"{requests['current']} current · [dim]{requests['last']} last[/dim]",
            )
        )
    rows.append(
        ("SOCKS proxy", _endpoint(status["proxy"]["ip"], status["proxy"]["port"]))
    )
    if config.http_proxy.enabled:
        rows.append(
            ("HTTP proxy", _endpoint(config.http_proxy.ip, config.http_proxy.port))
        )
    rows.extend(
        [
            ("API", _endpoint(config.api.ip, config.api.port)),
            (
                "Log file",
                str(config.logging.file) if config.logging.file else "disabled",
            ),
        ]
    )
    return rows


def _feedback_rows(status: dict, state: PanelState | None) -> list[tuple[str, str]]:
    """The rows that only appear once something has happened."""
    rows = []
    if state is not None and state.last_reload:
        rows.append(("Last reload", state.last_reload))
    if state is not None and state.last_action:
        rows.append(("Last action", state.last_action))
    if status["last_error"]:
        rows.append(("Last error", f"[red]{status['last_error']}[/]"))
    return rows


def _footer(config: Config, status: dict, state: PanelState | None, keys: bool) -> list:
    """The picker when it is open, the key footer otherwise, nothing without keys."""
    if not keys:
        return []
    if state is not None and state.picker_index is not None:
        # The picker takes over the footer: the normal keys mean nothing until
        # the open choice is confirmed or cancelled.
        return [
            _server_picker(config, status, state),
            *(Text(line, style="dim") for line in PICKER_FOOTER),
        ]
    return [Text(line, style="dim") for line in FOOTER]


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
    for label, value in _summary_rows(config, status):
        table.add_row(label, value)
    for label, value in _feedback_rows(status, state):
        table.add_row(label, value)
    return Panel(
        Group(table, *_footer(config, status, state, keys)),
        title=f"SSH Tunnel Switch {__version__}",
        border_style="blue",
    )


def _disable_flow_control(descriptor: int) -> None:
    """Drop software flow control so Ctrl+S can be a key.

    ``tty.setcbreak`` clears only ``ECHO`` and ``ICANON``: with ``IXON`` still
    set the line discipline eats Ctrl+S as XOFF and the panel freezes instead
    of opening the picker. The attributes ``_key_reader`` saved beforehand put
    flow control back on exit, so this lasts only while the panel is shown. A
    terminal that refuses the change keeps the old behaviour.
    """
    import termios

    try:
        mode = termios.tcgetattr(descriptor)
        mode[0] &= ~(termios.IXON | termios.IXANY | termios.IXOFF)
        termios.tcsetattr(descriptor, termios.TCSANOW, mode)
    except (OSError, termios.error):
        pass


def _key_reader():
    """Put stdin in cbreak mode, returning its descriptor and a restore call.

    cbreak disables echo and line buffering so single keys are readable, while
    ``ISIG`` is left enabled: Ctrl-C still raises SIGINT and uvicorn still
    performs its graceful shutdown. Software flow control is dropped
    separately, so Ctrl+S reaches the key map instead of pausing the output.
    Non-terminal and non-POSIX runs simply get no key handling.
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

    _disable_flow_control(descriptor)

    def restore():
        try:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass

    return descriptor, restore


def _dispatch(run, state: PanelState, action: str, *args) -> None:
    """Run one key action, reporting a failed dispatch in the panel."""
    try:
        run(*args)
    except OSError as exc:
        state.last_action = f"{action}: {exc}"


def _open_picker(state: PanelState, manager) -> None:
    """Highlight the server the tunnel uses, or the one a rotation would pick."""
    current = manager.status()["server"]
    state.picker_index = next(
        (
            index
            for index, server in enumerate(manager.config.servers)
            if server.name == current
        ),
        0,
    )


def _handle_picker(sequence: bytes, actions, state: PanelState, manager) -> None:
    """Move the highlight, or hand the chosen server to the select action."""
    servers = manager.config.servers if manager is not None else []
    step = PICKER_KEYS.get(sequence)
    if step is not None and servers:
        state.picker_index = (state.picker_index + step) % len(servers)
    elif sequence in (b"\r", b"\n"):
        index = state.picker_index
        # A reload may have shrunk the list while the picker was open, so the
        # highlight is validated before it names a server.
        chosen = servers[index] if 0 <= index < len(servers) else None
        state.picker_index = None
        if chosen is not None and actions is not None and "select" in actions:
            _dispatch(actions["select"], state, "select", chosen.name)
    elif sequence == b"\x1b":
        state.picker_index = None


def _handle_keys(descriptor: int, actions, state: PanelState, manager=None) -> None:
    sequence = os.read(descriptor, 32)
    # Alt+R may arrive as ESC and "r" in separate reads.
    if (
        sequence.endswith(b"\x1b")
        and select.select([descriptor], [], [], ESCAPE_WAIT_SECONDS)[0]
    ):
        sequence += os.read(descriptor, 8)
    if state.picker_index is not None:
        _handle_picker(sequence, actions, state, manager)
        return
    action = decode_key(sequence)
    if action == "select":
        # Opening the picker belongs to this display thread; only the server
        # confirmed inside it is dispatched to the event loop.
        if manager is not None and actions is not None and "select" in actions:
            _open_picker(state, manager)
        return
    if action is None or actions is None or action not in actions:
        return
    _dispatch(actions[action], state, action)


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
                    _handle_keys(descriptor, actions, state, manager)
    finally:
        restore()
