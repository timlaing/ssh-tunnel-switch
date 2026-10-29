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

The select key opens an inline picker instead of dispatching at once: it
pops up over the dashboard, starting below its title border, listing the
configured servers in a window sized to the display, arrow keys (or
``j``/``k``) move the highlight — scrolling the window with it — Enter hands
the highlighted server to the select action and Esc closes the picker
without changing anything.
"""

from datetime import datetime, timezone
import logging
import os
import select
from threading import Event

from rich.console import Console, Group, Segment
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Config

logger = logging.getLogger(__name__)

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
# Short enough to stay a single line on a normal terminal: the picker's row
# budget counts each of these strings as one line of the display.
PICKER_FOOTER = ("↑/↓ or j/k: highlight · Enter: connect · Esc: cancel",)
# Picker navigation, matched as one complete read: arrows and j/k move the
# highlight, Enter and Esc resolve it. Every other byte is ignored.
PICKER_KEYS = {b"\x1b[A": -1, b"k": -1, b"\x1b[B": 1, b"j": 1}
# Lines the picker pop-up spends around its server list: its own top and
# bottom border. It starts one row down, so the dashboard's title border
# stays readable above it; together with the key hints these are the rows the
# display budget subtracts to end the pop-up on its last line, never below.
PICKER_EDGE_LINES = 2
PICKER_OFFSET_LINES = 1


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
        # First server shown by the picker's scroll window, moved by rendering
        # so the highlight stays visible within the display.
        self.picker_offset = 0


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


class _Overlay:
    """One renderable drawn over another's rows, the way a pop-up covers a dashboard.

    Rich has no absolute positioning, so both renderables are rendered against
    the same console and the pop-up's lines replace the panel's rows beneath
    them, starting ``offset`` rows down. Rows the pop-up does not reach keep
    their panel content: the dashboard stays readable around it.

    ``limit`` is the largest number of rows the finished composition may use.
    The row budget counts logical lines, but Rich folds a long server entry
    into two physical rows, the floor in ``_picker_rows`` keeps one server
    even when the display is too short for it, and a small display may hold
    fewer rows than the dashboard alone: without this cut the result could
    run past the display's last line and make ``Live`` scroll. What does not
    fit is dropped rather than pushed below the display.
    """

    def __init__(self, base, popup, offset: int = 0, limit: int | None = None):
        self.base = base
        self.popup = popup
        self.offset = offset
        self.limit = limit

    def __rich_console__(self, console, options):
        lines = list(Segment.split_lines(console.render(self.base, options)))
        popup = list(Segment.split_lines(console.render(self.popup, options)))
        for index, line in enumerate(popup):
            row = self.offset + index
            covered = Segment.adjust_line_length(line, options.max_width)
            if row < len(lines):
                lines[row] = covered
            else:
                lines.append(covered)
        if self.limit is not None:
            lines = lines[: self.limit]
        # split_lines drops the line breaks, so each one is put back: without
        # them the console sees one very long line and writes its first row.
        for line in lines:
            yield from line
            yield Segment("\n")


def _picker_rows(height: int | None, used: int) -> int | None:
    """Server rows left over in a display ``height`` rows tall.

    ``used`` counts every line the pop-up spends around the list: the offset
    that keeps the dashboard's title visible, its borders and its key hints.
    The list gets the rest of the dashboard rather than the window, so a long
    server list scrolls inside the display instead of pushing past its last
    line. A very short display still gets one row, so the picker keeps
    working.
    """
    if height is None:
        return None
    return max(1, height - used)


def _picker_window(state: PanelState, total: int, rows: int | None) -> tuple[int, int]:
    """Choose the slice of ``total`` servers a window of ``rows`` rows shows.

    The window keeps the highlight inside it and moves no further than it
    must: scrolling past the last visible server pushes the window down by
    one, scrolling back above the first pulls it up again. A shorter list (a
    reload) or a shorter display pulls the window in with it — the highlight
    and the window are both clamped to the new bounds, so a reload that
    shrinks the list below the highlighted index cannot send the window past
    the end of the list and render servers that no longer exist.
    """
    if rows is None or rows >= total:
        state.picker_offset = 0
        return 0, total
    index = min(state.picker_index or 0, total - 1)
    offset = min(state.picker_offset, total - rows)
    if index >= offset + rows:
        offset = index - rows + 1
    elif index < offset:
        offset = index
    state.picker_offset = max(0, min(offset, total - rows))
    return state.picker_offset, state.picker_offset + rows


def _server_picker(
    config: Config, status: dict, state: PanelState, rows: int | None = None
) -> Panel:
    """The picker as a pop-up: the servers in the window that fits the display.

    The window follows the highlight, and the title names how many servers
    are hidden above and below it, so a scrolled-away part of the list is
    never silently missing. The key hints sit inside the same box, under the
    list, so they stay with the picker wherever it lands.
    """
    current = status["server"]
    servers = config.servers
    offset, end = _picker_window(state, len(servers), rows)
    hidden = []
    if offset:
        hidden.append(f"{offset} more above")
    if end < len(servers):
        hidden.append(f"{len(servers) - end} more below")
    title = "Select server"
    if hidden:
        title += f" ({', '.join(hidden)})"
    lines = []
    for index in range(offset, end):
        server = servers[index]
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
    lines.extend(Text(line, style="dim") for line in PICKER_FOOTER)
    return Panel(Group(*lines), title=title, border_style="cyan")


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


def _footer(state: PanelState | None, keys: bool) -> list:
    """The key footer, hidden while the open picker owns the keyboard."""
    if not keys or (state is not None and state.picker_index is not None):
        return []
    return [Text(line, style="dim") for line in FOOTER]


def status_panel(
    config: Config,
    status: dict,
    state: PanelState | None = None,
    keys: bool = False,
    height: int | None = None,
) -> Panel | _Overlay:
    """Render the current tunnel state as a single status panel.

    An open picker is drawn as a pop-up over the dashboard rather than a list
    beneath it: the panel keeps its rows, the picker replaces the ones it
    covers starting below the title border, and its window is sized to the
    display so the pair ends on the display's last line. ``height`` is that
    display's height in rows; a caller that passes none (a test, a plain
    render) lists every server in the pop-up.
    """
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="left")
    table.add_column(justify="left")
    for label, value in (
        *_summary_rows(config, status),
        *_feedback_rows(status, state),
    ):
        table.add_row(label, value)
    panel = Panel(
        Group(table, *_footer(state, keys)),
        title=f"SSH Tunnel Switch {__version__}",
        border_style="blue",
    )
    if not keys or state is None or state.picker_index is None:
        return panel
    rows = _picker_rows(
        height, PICKER_OFFSET_LINES + PICKER_EDGE_LINES + len(PICKER_FOOTER)
    )
    # The composition may use every row of the display and no more, so
    # wrapped or over-budget content is cut at the last line, not below it.
    return _Overlay(
        panel,
        _server_picker(config, status, state, rows),
        PICKER_OFFSET_LINES,
        height,
    )


def _disable_flow_control(descriptor: int) -> None:
    """Drop software flow control so Ctrl+S can be a key.

    ``tty.setcbreak`` clears only ``ECHO`` and ``ICANON``: with ``IXON`` still
    set the line discipline eats Ctrl+S as XOFF and the panel freezes instead
    of opening the picker. The attributes ``_key_reader`` saved beforehand put
    flow control back on exit, so this lasts only while the panel is shown.

    The mask is built from the flags this build of ``termios`` actually
    defines, and nothing here raises: the call runs after cbreak is already on
    and before ``_key_reader`` returns, so an escaping exception would strand
    the terminal with no echo and cost the panel its keyboard. A terminal that
    refuses the change keeps the old behaviour, with the reason reported — a
    Ctrl+S that only pauses the output is otherwise indistinguishable from one
    the panel never received.
    """
    import termios

    mask = 0
    for flag in ("IXON", "IXANY", "IXOFF"):
        mask |= getattr(termios, flag, 0)
    if not mask:
        logger.warning(
            "This build of termios has no software flow-control flags; Ctrl+S "
            "pauses the output instead of opening the server picker."
        )
        return
    try:
        mode = termios.tcgetattr(descriptor)
        mode[0] &= ~mask
        termios.tcsetattr(descriptor, termios.TCSANOW, mode)
    except (OSError, termios.error) as exc:
        logger.warning(
            "Could not disable software flow control (%s); Ctrl+S pauses the "
            "output instead of opening the server picker.",
            exc,
        )


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
    state.picker_offset = 0


def _close_picker(state: PanelState) -> None:
    """Dismiss the picker, taking its scroll window back to the top."""
    state.picker_index = None
    state.picker_offset = 0


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
        _close_picker(state)
        if chosen is not None and actions is not None and "select" in actions:
            _dispatch(actions["select"], state, "select", chosen.name)
    elif sequence == b"\x1b":
        _close_picker(state)


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

    def panel():
        # The display's height, not the window's, caps the open picker: the
        # panel is what the terminal shows, so the list scrolls inside it.
        return status_panel(
            manager.config,
            manager.status(),
            state,
            keys=keys,
            height=console.height,
        )

    try:
        with Live(
            panel(),
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
                live.update(panel())
                if stop.is_set():
                    break
                if readable:
                    _handle_keys(descriptor, actions, state, manager)
    finally:
        restore()
