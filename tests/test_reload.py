"""Live configuration reload, its API surface and the interactive key map."""

import asyncio
import itertools
import json
import logging
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from rich.console import Console

from tunnel_switch import __version__, status
from tunnel_switch import api as api_module
from tunnel_switch import reload as reload_module
from tunnel_switch.api import create_app
from tunnel_switch.cli import _cli_overrides, _reload_feedback, key_actions
from tunnel_switch.config import Config, load_config
from tunnel_switch.reload import ConfigReloader, ReloadOutcome
from tunnel_switch.status import (
    PanelState,
    _elapsed,
    _key_reader,
    decode_key,
    status_panel,
)
from tunnel_switch.tunnel import TunnelManager


@pytest.fixture
def terminal_modules() -> tuple[ModuleType, ModuleType]:
    """Require POSIX terminal modules only for tests that exercise terminal setup."""
    return pytest.importorskip("termios"), pytest.importorskip("tty")


def document(**changes: object) -> str:
    """Serialize a minimal two-server configuration with requested overrides."""
    base = {
        "servers": [
            {"name": "one", "host": "one.example", "user": "tunnel"},
            {"name": "two", "host": "two.example", "user": "tunnel"},
        ],
        "proxy": {"ip": "127.0.0.1", "port": 1080},
        "api": {"token": "secret"},
        "start_on_launch": False,
        "state_file": None,
        "cooldown_seconds": 0,
        "http_proxy": {"enabled": False},
        "logging": {"file": None},
    }
    base.update(changes)
    return json.dumps(base)


def config(**changes: object) -> Config:
    """Validate the serialized configuration used by reload and panel tests."""
    return Config.model_validate(json.loads(document(**changes)))


def many_servers(count: int) -> Config:
    """Build a validated config too large for a short terminal display."""
    return config(
        servers=[
            {"name": f"s{index:02}", "host": f"host{index}.example", "user": "tunnel"}
            for index in range(count)
        ]
    )


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    """Write a reloadable configuration into the test's temporary directory."""
    path = tmp_path / "config.yaml"
    path.write_text(document(), encoding="utf-8")
    return path


class Process:
    """A fake ssh subprocess that stays up until terminated."""

    returncode: int | None = None

    def __init__(self) -> None:
        """Start the fake SSH process without an exit status."""
        self.terminated = False

    def terminate(self) -> None:
        """Record a graceful termination and successful exit status."""
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        """Record the exit status of a forcibly killed process."""
        self.returncode = -9

    async def wait(self) -> int | None:
        """Return the fake process's current exit status."""
        return self.returncode


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> TunnelManager:
    """Build a tunnel manager with a fake SSH process and SOCKS socket."""
    manager = TunnelManager(config())
    monkeypatch.setattr(manager, "_ensure_socket_free", lambda: None)
    monkeypatch.setattr(manager, "_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=lambda *_args, **_kwargs: Process()),
    )
    return manager


async def until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    """Wait for an asynchronous test condition, failing on a bounded timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition was not met in time")
        await asyncio.sleep(0.01)


async def test_reload_applies_hot_fields_and_defers_binds(yaml_file: Path) -> None:
    """Apply hot fields while preserving all running socket binds."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(
        document(
            servers=[
                {"name": "one", "host": "one.example", "user": "tunnel"},
                {"name": "two", "host": "two.example", "user": "tunnel"},
                {"name": "three", "host": "three.example", "user": "tunnel"},
            ],
            proxy={"ip": "127.0.0.1", "port": 1090},
            api={"token": "secret", "port": 9000},
            cooldown_seconds=45,
        )
    )
    outcome = await reloader.reload_now()
    assert outcome.applied, "Hot-field reload must apply"
    assert outcome.error is None, "Successful reload must not report an error"
    assert [server.name for server in manager.config.servers] == [
        "one",
        "two",
        "three",
    ], "Reload must adopt all three configured servers"
    assert manager.config.cooldown_seconds == 45, "Reload must adopt the cooldown"
    # The bound sockets keep their running values; nothing is rebound.
    assert manager.config.proxy.port == 1080, "Running SOCKS bind must remain unchanged"
    assert manager.config.api.port == 8765, "Running API bind must remain unchanged"
    assert manager.config.api.token == "secret", "Reload must retain the API token"
    assert reloader.last_error is None, "Reloader must clear its last error"
    assert any(entry.startswith("proxy bind") for entry in outcome.deferred), (
        "SOCKS bind change must be deferred"
    )
    assert any(entry.startswith("API bind") for entry in outcome.deferred), (
        "API bind change must be deferred"
    )
    assert reloader.pending is not None, (
        "Deferred binds must retain pending configuration"
    )
    await manager.close()


async def test_reload_defers_an_http_proxy_bind_change(yaml_file: Path) -> None:
    """Keep a disabled HTTP listener disabled until its bind can be adopted."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(document(http_proxy={"enabled": True, "port": 8118}))
    outcome = await reloader.reload_now()
    assert outcome.applied, "HTTP bind reload must apply its hot fields"
    # The listener was never started for it, so enabling stays deferred and the
    # status keeps reporting the socket that is actually bound (none).
    assert manager.config.http_proxy.enabled is False, (
        "HTTP listener must remain disabled"
    )
    assert any(entry.startswith("HTTP proxy bind") for entry in outcome.deferred), (
        "HTTP bind change must be deferred"
    )
    await manager.close()


async def test_reload_keeps_running_config_when_yaml_is_invalid(
    yaml_file: Path,
) -> None:
    """Report invalid YAML without replacing the running configuration."""
    cfg = load_config(yaml_file)
    outcomes = []
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(
        yaml_file, cfg, targets=[manager], hooks=[outcomes.append]
    )
    yaml_file.write_text("servers: []\n")
    outcome = await reloader.reload_now()
    assert outcome.error, "Invalid configuration must report an error"
    assert not outcome.applied, "Invalid configuration must not apply"
    assert [server.name for server in manager.config.servers] == ["one", "two"], (
        "Invalid reload must preserve the running servers"
    )
    # The failure is reported to /status and the panel, never raised at startup.
    assert reloader.last_error == outcome.error, "Status must retain the reload error"
    assert outcomes == [outcome], "Failure hook must receive exactly the failed outcome"
    await manager.close()


async def test_reload_with_unchanged_content_is_not_applied(yaml_file: Path) -> None:
    """Treat a same-content rewrite as unchanged rather than applied."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(document())  # A rewrite with nothing to apply.
    outcome = await reloader.reload_now()
    assert outcome.unchanged, "Same-content rewrite must be unchanged"
    assert not outcome.applied, "Unchanged reload must not apply"
    assert reloader.last_reload is None, (
        "Unchanged reload must not create an applied timestamp"
    )
    await manager.close()


async def test_pending_bind_is_adopted_by_the_next_connection_change(
    yaml_file: Path,
) -> None:
    """Adopt and clear a deferred SOCKS bind before the next connection."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(document(proxy={"ip": "127.0.0.1", "port": 1090}))
    await reloader.reload_now()
    assert manager.config.proxy.port == 1080, (
        "Reload alone must retain the running SOCKS port"
    )
    adopted = reloader.apply_pending_binds()
    assert adopted.proxy.port == 1090, (
        "Pending configuration must carry the desired port"
    )
    # A rotation reads the adopted bind when it starts ssh.
    assert manager.config.proxy.port == 1090, (
        "Adoption must update the manager's SOCKS port"
    )
    assert reloader.pending is None, "Adoption must clear pending configuration"
    assert not any(entry.startswith("proxy bind") for entry in reloader.deferred), (
        "Adopted SOCKS bind must leave the deferred list"
    )
    await manager.close()


async def test_reload_defers_while_a_rotation_is_in_progress(yaml_file: Path) -> None:
    """Refuse a busy reload without changing configuration or firing hooks."""
    cfg = load_config(yaml_file)
    reached = []
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(
        yaml_file, cfg, targets=[manager], hooks=[reached.append], busy=lambda: True
    )
    yaml_file.write_text(document(cooldown_seconds=99))
    outcome = await reloader.reload_now()
    assert outcome.refused, "Busy reload must be refused"
    assert outcome.error == "rotation in progress", (
        "Busy refusal must explain the rotation"
    )
    assert manager.config.cooldown_seconds == 0, (
        "Busy reload must preserve the cooldown"
    )
    # A refused reload never reaches the panel or the logging hook.
    assert not reached, "Refused reload must not invoke reporting hooks"
    await manager.close()


async def test_poller_retries_a_reload_refused_during_a_rotation(
    yaml_file: Path,
) -> None:
    """A refused tick keeps the old signature, so the next tick tries again."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    gate = {"busy": True}
    reloader = ConfigReloader(
        yaml_file, cfg, targets=[manager], interval=0.05, busy=lambda: gate["busy"]
    )
    yaml_file.write_text(document(cooldown_seconds=99))
    stop = asyncio.Event()
    task = asyncio.create_task(reloader.run(stop))
    try:
        await asyncio.sleep(0.3)  # Several refused (or debouncing) ticks.
        assert manager.config.cooldown_seconds == 0, (
            "Refused poll ticks must retain the old cooldown"
        )
        gate["busy"] = False  # The rotation finishes; the change must land.
        await until(lambda: manager.config.cooldown_seconds == 99)
    finally:
        stop.set()
        await task
    await manager.close()


async def test_reload_refuses_when_a_rotation_starts_during_validation(
    yaml_file: Path,
) -> None:
    """The file is read in a thread, so the busy gate is re-checked after it."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    checks = []

    def busy() -> bool:
        checks.append(True)
        return len(checks) > 1  # Idle at entry, rotating once the read returns.

    reloader = ConfigReloader(yaml_file, cfg, targets=[manager], busy=busy)
    yaml_file.write_text(document(cooldown_seconds=99))
    outcome = await reloader.reload_now()
    assert outcome.refused, "Rotation during validation must refuse the reload"
    assert manager.config.cooldown_seconds == 0, (
        "Refused validation must preserve configuration"
    )
    assert reloader.last_reload is None, (
        "Refused validation must not record an applied timestamp"
    )
    await manager.close()


async def test_unchanged_reload_clears_a_stale_error(yaml_file: Path) -> None:
    """A file that validates again must not keep reporting the old failure."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text("servers: [")  # Broken mid-edit.
    failed = await reloader.reload_now()
    assert failed.error, "Broken YAML must fail validation"
    assert reloader.last_error is not None, "Broken YAML must populate the last error"
    yaml_file.write_text(document())  # Repaired back to the running config.
    outcome = await reloader.reload_now()
    assert outcome.unchanged, "Repair to running configuration must be unchanged"
    assert reloader.last_error is None, (
        "Valid unchanged configuration must clear stale errors"
    )
    await manager.close()


async def test_concurrent_reloads_run_one_at_a_time(
    yaml_file: Path, manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued reload waits for the running one, so neither applies a stale read."""
    real_load = reload_module.load_config
    entered, release, calls = Event(), Event(), []
    reads = itertools.count()

    def ordered_load(path: Path) -> Config:
        if next(reads) == 0:
            entered.set()  # The first read holds the reload open.
            release.wait(timeout=5)
        calls.append(True)
        return real_load(path)

    monkeypatch.setattr(reload_module, "load_config", ordered_load)
    reloader = ConfigReloader(yaml_file, load_config(yaml_file), targets=[manager])
    yaml_file.write_text(document(cooldown_seconds=99))
    first = asyncio.create_task(reloader.reload_now())
    await until(lambda: entered.is_set())
    second = asyncio.create_task(reloader.reload_now())
    try:
        await asyncio.sleep(0.1)
        # The queued reload must not have read the file while the first
        # reload still holds it: overlapping reads are what corrupts the
        # signature-to-content match.
        assert calls == [], (
            "Queued reload must not read before the first releases its lock"
        )
        release.set()
        await asyncio.gather(first, second)
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    assert calls == [True, True], "Both serialized reloads must perform their reads"
    assert manager.config.cooldown_seconds == 99, (
        "Serialized reloads must adopt the desired cooldown"
    )
    await manager.close()


async def test_poller_picks_up_a_changed_file(yaml_file: Path) -> None:
    """Detect a changed file and replace the running server list."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager], interval=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(reloader.run(stop))
    yaml_file.write_text(
        document(servers=[{"name": "solo", "host": "solo.example", "user": "tunnel"}])
    )
    try:
        await until(lambda: manager.config.servers[0].name == "solo")
    finally:
        stop.set()
        await task
    assert [server.name for server in manager.config.servers] == ["solo"], (
        "Poller must replace the complete server list"
    )
    await manager.close()


async def test_reload_keeps_cli_overrides_for_the_life_of_the_process(
    yaml_file: Path,
) -> None:
    """Apply YAML hot changes without replacing the CLI state-file override."""
    cfg = load_config(yaml_file)
    cfg.state_file = yaml_file.parent / "cli.json"
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(
        yaml_file,
        cfg,
        targets=[manager],
        prepare=lambda desired: setattr(desired, "state_file", cfg.state_file),
    )
    yaml_file.write_text(document(state_file="yaml-state.json", cooldown_seconds=99))
    outcome = await reloader.reload_now()
    assert outcome.applied, "Reload with CLI overrides must apply"
    assert manager.config.cooldown_seconds == 99, (
        "CLI overrides must not block hot cooldown changes"
    )
    # The CLI override wins for this run; the YAML value applies after a restart.
    assert manager.config.state_file == yaml_file.parent / "cli.json", (
        "CLI state-file override must outlive reload"
    )
    await manager.close()


async def test_reload_endpoint_reports_and_applies(yaml_file: Path) -> None:
    """Expose watching metadata and report a successful authenticated reload."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    app = create_app(cfg, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = (await client.get("/status", headers=headers)).json()
        assert status["config_reload"]["watching"] is True, (
            "Status must report active configuration watching"
        )
        assert status["config_reload"]["path"] == str(yaml_file), (
            "Status must report the watched path"
        )
        assert status["config_reload"]["deferred"] == [], (
            "Initial status must have no deferred changes"
        )
        yaml_file.write_text(
            document(
                servers=[{"name": "one", "host": "one.example", "user": "tunnel"}],
                cooldown_seconds=45,
            )
        )
        response = await client.post("/reload", headers=headers)
        assert response.status_code == 200, "Authenticated reload must succeed"
        payload = response.json()
        assert payload["applied"], "Reload response must report applied changes"
        assert payload["servers"] == 1, (
            "Reload response must report the new server count"
        )
        assert payload["error"] is None, "Successful response must have no reload error"
        status = (await client.get("/status", headers=headers)).json()
        assert status["config_reload"]["last_reload"], (
            "Status must record the successful reload"
        )
        assert status["config_reload"]["last_error"] is None, (
            "Status must not retain an error after success"
        )
    await manager.close()


async def test_reload_endpoint_is_guarded(yaml_file: Path) -> None:
    """Reject unauthenticated and busy reload requests with their HTTP guards."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    app = create_app(cfg, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/reload")).status_code == 401, (
            "Reload must require authentication"
        )
        manager._busy = True
        rejected = await client.post("/reload", headers=headers)
        assert rejected.status_code == 429, "Busy reload must return HTTP 429"
        assert rejected.headers["Retry-After"] == "1", (
            "Busy reload must include its retry delay"
        )
        manager._busy = False
    await manager.close()


async def test_reload_without_a_config_path_is_not_available(
    manager: TunnelManager,
) -> None:
    """Disable reload and watching metadata when no configuration path exists."""
    app = create_app(manager.config, "secret", manager)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post("/reload", headers=headers)
        assert missing.status_code == 404, (
            "Reload without a configuration path must be unavailable"
        )
        status = (await client.get("/status", headers=headers)).json()
        assert status["config_reload"]["watching"] is False, (
            "Pathless application must not report watching"
        )
        assert status["config_reload"]["path"] is None, (
            "Pathless application must not invent a configuration path"
        )
    await manager.close()


async def test_reload_rotates_the_bearer_token(yaml_file: Path) -> None:
    """Replace the bearer token immediately after the old token reloads it."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    app = create_app(cfg, "secret", manager, config_path=yaml_file)
    old = {"Authorization": "Bearer secret"}
    new = {"Authorization": "Bearer rotated"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yaml_file.write_text(document(api={"token": "rotated"}))
        # The old token triggers the reload, then stops working at once.
        assert (await client.post("/reload", headers=old)).status_code == 200, (
            "Old token must authorize its own replacement"
        )
        assert (await client.post("/reload", headers=old)).status_code == 401, (
            "Old token must stop authorizing reload immediately"
        )
        assert (await client.post("/reload", headers=new)).status_code == 200, (
            "Replacement token must authorize reload"
        )
        assert (await client.get("/status", headers=new)).status_code == 200, (
            "Replacement token must authorize status"
        )
    await manager.close()


@pytest.mark.parametrize(
    "sequence,action",
    [
        (b"\x12", "reload"),  # Ctrl+R
        (b"\x1br", "restart"),  # Alt+R
        (b"\x0e", "rotate"),  # Ctrl+N
        (b"\x05", "connect"),  # Ctrl+E
        (b"\x04", "disconnect"),  # Ctrl+D
        (b"\x1bs", "select"),  # Alt+S
        (b"\x13", "select"),  # Ctrl+S, for a terminal whose Option key types
        (b"x\x12", "reload"),  # A key read may carry extra bytes.
    ],
)
def test_decode_key_maps_the_control_keys(sequence: bytes, action: str) -> None:
    """Map supported terminal sequences, including bundled key reads."""
    assert decode_key(sequence) == action, (
        f"Key sequence {sequence!r} must map to {action!r}"
    )


@pytest.mark.parametrize(
    "sequence",
    [b"", b"\x03", b"\x1b", b"\x1b[C", b"\x1bx", b"plain text"],  # Ctrl+C stays SIGINT
)
def test_decode_key_ignores_everything_else(sequence: bytes) -> None:
    """Ignore unsupported keys and leave Ctrl+C to signal handling."""
    assert decode_key(sequence) is None, (
        f"Unsupported sequence {sequence!r} must be ignored"
    )


def test_status_panel_shows_feedback_and_the_key_footer() -> None:
    """Render feedback and interactive shortcuts only when appropriate."""
    cfg = config()
    state = PanelState()
    state.last_reload = "12:00:01 applied (2 servers)"
    state.last_action = "rotate: two"
    with_keys = rendered(cfg, state=state, keys=True)
    assert "Last reload" in with_keys, "Panel must label reload feedback"
    assert "12:00:01 applied (2 servers)" in with_keys, (
        "Panel must show the reload outcome"
    )
    assert "Last action" in with_keys, "Panel must label action feedback"
    assert "rotate: two" in with_keys, "Panel must show the last action"
    assert "Ctrl+R: reload config" in with_keys, (
        "Interactive footer must show reload shortcut"
    )
    assert "Ctrl+N: rotate server" in with_keys, (
        "Interactive footer must show rotation shortcut"
    )
    assert "Alt+S/Ctrl+S: select server" in with_keys, (
        "Interactive footer must show selection shortcuts"
    )
    without_keys = rendered(cfg, state=state)
    assert "Ctrl+R" not in without_keys, (
        "Noninteractive panel must omit keyboard shortcuts"
    )
    # Without feedback the rows are omitted entirely.
    assert "Last reload" not in rendered(cfg), (
        "Panel without feedback must omit reload rows"
    )


def test_status_panel_renders_the_open_server_picker() -> None:
    """Replace normal shortcuts with picker controls and current-server markers."""
    cfg = config()
    state = PanelState()
    state.picker_index = 1
    picking = rendered(cfg, state=state, keys=True, server="one")
    assert "Select server" in picking, "Open picker must display its title"
    assert "❯ two" in picking, "Picker must mark the highlighted server"
    assert "· current" in picking, "Picker must mark the connected server"
    assert "Enter: connect" in picking, "Picker must explain confirmation"
    # The picker replaces the ordinary keys while it owns the keyboard.
    assert "Ctrl+N: rotate server" not in picking, (
        "Picker must replace normal shortcuts"
    )
    # Nothing could confirm a list without terminal keys, so it stays hidden.
    assert "Select server" not in rendered(cfg, state=state), (
        "Noninteractive panel must hide the picker"
    )


def test_status_panel_picker_scrolls_inside_the_display() -> None:
    """Scroll a long server list within the terminal's physical row budget."""
    cfg = many_servers(24)
    state = PanelState()
    state.picker_index = 0
    top = rendered(cfg, state=state, keys=True, height=12)
    shown = re.findall(r"\bs\d\d\b", top)
    assert shown, "Top picker window must contain servers"
    assert shown[0] == "s00", "Top picker window must start at the highlighted server"
    assert shown == [f"s{index:02}" for index in range(len(shown))], (
        "Visible top servers must remain contiguous"
    )
    assert len(shown) < 24, "Picker must scroll rather than list every server"
    # With 24 servers and an eight-row window, exactly 16 hide below.
    assert "(16 more below)" in top, "Top window must count all sixteen hidden servers"
    assert "more above" not in top, "Top window must not claim hidden servers above"
    assert len(top.splitlines()) <= 12, "Top picker must fit the display"
    state.picker_index = 23
    bottom = rendered(cfg, state=state, keys=True, height=12)
    shown = re.findall(r"\bs\d\d\b", bottom)
    assert "s23" in shown, "Bottom window must follow the highlighted server"
    assert len(shown) < 24, "Bottom window must still scroll its list"
    assert "(16 more above)" in bottom, (
        "Bottom window must count all sixteen hidden servers"
    )
    assert len(bottom.splitlines()) <= 12, "Bottom picker must fit the display"


def test_status_panel_picker_pops_up_over_the_dashboard() -> None:
    """Overlay dashboard rows without pushing the popup outside the display."""
    cfg = many_servers(24)
    state = PanelState()
    state.picker_index = 0
    picking = rendered(cfg, state=state, keys=True, height=16)
    assert "SSH Tunnel Switch" in picking, "Dashboard title must remain above the popup"
    # The dashboard's rows are covered by the picker, not pushed down by it.
    assert "SOCKS proxy" not in picking, "Picker must cover dashboard rows"
    assert len(picking.splitlines()) == 16, "Popup must end at the last display line"


def test_status_panel_picker_follows_the_highlight_a_row_at_a_time() -> None:
    """Follow upward selection with monotonic, single-row scrolling."""
    cfg = many_servers(24)
    state = PanelState()
    state.picker_index = 23
    first = rendered(cfg, state=state, keys=True, height=12)  # Scrolls it in.
    assert "s23" in first, "First render must already include the highlighted server"
    assert state.picker_offset > 0, (
        "Bottom highlight must create a positive scroll offset"
    )
    lowest = state.picker_offset
    for index in range(22, -1, -1):
        state.picker_index = index
        picking = rendered(cfg, state=state, keys=True, height=12)
        shown = re.findall(r"\bs\d\d\b", picking)
        assert f"s{index:02}" in shown, (
            f"Highlighted server s{index:02} must remain visible"
        )
        # It scrolls a row at a time in either direction, never backwards.
        assert 0 <= lowest - state.picker_offset <= 1, (
            "Upward selection must scroll monotonically at most one row"
        )
        lowest = state.picker_offset
    assert state.picker_offset == 0, (
        "Returning to the first server must restore the top window"
    )


def test_status_panel_picker_keeps_one_row_in_a_tiny_display() -> None:
    """Keep one selectable server visible even in a four-line display."""
    cfg = many_servers(12)
    state = PanelState()
    state.picker_index = 0
    picking = rendered(cfg, state=state, keys=True, height=4)
    assert "s00" in picking, "Tiny display must retain the first server"
    assert "s01" not in picking, "Tiny display must not add a second server"
    # The budget's one-row floor may overrun, but the cut keeps it inside.
    assert len(picking.splitlines()) <= 4, (
        "Tiny picker must be cut to the display height"
    )


def test_status_panel_picker_clamps_a_highlight_a_reload_shrunk() -> None:
    """Clamp a stale picker highlight to the shortened server list."""
    # A reload can leave the highlight past the end of the new, shorter list;
    # the window must land inside that list, not render servers that are gone.
    state = PanelState()
    state.picker_index = 23
    state.picker_offset = 16
    picking = rendered(many_servers(5), state=state, keys=True, height=8)
    shown = re.findall(r"\bs\d\d\b", picking)
    assert shown == [f"s{index:02}" for index in range(1, 5)], (
        "Shrunken list must show only the expected surviving servers"
    )
    assert len(picking.splitlines()) <= 8, "Clamped picker must fit the display"


def test_status_panel_picker_is_cut_at_the_display_when_lines_wrap() -> None:
    """Clip wrapped server entries at the terminal's physical final line."""
    # Rich folds a long server entry into two physical rows, which the
    # logical row budget cannot see: the fold must be cut at the last line
    # rather than push the pop-up below the display.
    cfg = config(
        servers=[
            {
                "name": f"s{index:02}",
                "host": f"long-host-{index:02}.example.internal",
                "user": "tunnel",
            }
            for index in range(12)
        ]
    )
    state = PanelState()
    state.picker_index = 0
    picking = rendered(cfg, state=state, keys=True, height=10, width=40)
    assert "s00" in picking, "Wrapped picker must retain its highlighted server"
    assert len(picking.splitlines()) <= 10, "Wrapped rows must not overflow the display"


def test_status_panel_picker_lists_every_server_without_a_display_height() -> None:
    """List all servers when no terminal height constrains the rendering."""
    # A caller with no display to fit (a plain render, a test) lists them all.
    cfg = many_servers(12)
    state = PanelState()
    state.picker_index = 11
    picking = rendered(cfg, state=state, keys=True)
    assert "more above" not in picking, "Unbounded picker must not claim hidden rows"
    assert all(f"s{index:02}" in picking for index in range(12)), (
        "Unbounded picker must show all twelve servers"
    )


def test_status_panel_title_carries_the_version() -> None:
    """Include the running package version in the dashboard title."""
    assert f"SSH Tunnel Switch {__version__}" in rendered(config()), (
        "Dashboard title must include the package version"
    )


def rendered(
    cfg: Config,
    state: PanelState | None = None,
    *,
    keys: bool = False,
    server: str | None = None,
    height: int | None = None,
    width: int = 100,
) -> str:
    """Render a disconnected dashboard into a bounded in-memory terminal."""
    status = {
        "state": "disconnected",
        "connection_id": None,
        "started_at": None,
        "server": server,
        "proxy": {"ip": "127.0.0.1", "port": 1080},
        "last_error": None,
    }
    buffer = StringIO()
    Console(file=buffer, width=width, height=height).print(
        status_panel(cfg, status, state=state, keys=keys, height=height)
    )
    return buffer.getvalue()


async def test_connect_key_is_a_hint_when_already_connected(
    manager: TunnelManager,
) -> None:
    """Offer a restart hint instead of reconnecting an already active tunnel."""
    await manager.rotate()
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    assert sorted(actions) == [
        "connect",
        "disconnect",
        "reload",
        "restart",
        "rotate",
        "select",
    ], "Keyboard actions must expose all six supported commands"
    actions["connect"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "already connected to one; Alt+R restarts it", (
        "Connect on an active tunnel must explain the restart shortcut"
    )
    await manager.close()


async def test_connect_key_reconnects_when_disconnected(manager: TunnelManager) -> None:
    """Reconnect the previously selected server after a disconnect."""
    await manager.rotate()
    await manager.disconnect()
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["connect"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "connect: tunnel up on one", (
        "Connect must report the reconnected server"
    )
    await manager.close()


async def test_disconnect_key_reports_a_rejected_connection_change(
    manager: TunnelManager,
) -> None:
    """Report busy connection changes in keyboard feedback."""
    manager._busy = True
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["disconnect"]()
    await until(lambda: panel.last_action is not None)
    assert "in progress" in panel.last_action, (
        "Rejected disconnect must explain the in-progress change"
    )
    manager._busy = False
    await manager.close()


async def test_restart_key_reports_the_remaining_cooldown(
    manager: TunnelManager,
) -> None:
    """Show the remaining cooldown when a restart is rejected."""
    manager.config.cooldown_seconds = 30
    await manager.rotate()
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["restart"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action.startswith(
        "restart: Tunnel rotation cooldown active (retry in "
    ), "Restart feedback must report the remaining cooldown"
    await manager.close()


async def test_rotate_key_skips_the_cooldown(manager: TunnelManager) -> None:
    """Allow an explicit keyboard rotation to bypass the API cooldown."""
    manager.config.cooldown_seconds = 30
    await manager.rotate()
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["rotate"]()
    await until(lambda: panel.last_action is not None)
    # The API answers 429 here; the key the operator just pressed does not.
    assert panel.last_action == "rotate: two", (
        "Keyboard rotation must bypass cooldown and reach the next server"
    )
    await manager.close()


async def test_select_key_connects_to_the_chosen_server(manager: TunnelManager) -> None:
    """Connect and report the server explicitly chosen by the operator."""
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["select"]("two")
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "select: two", (
        "Selection feedback must name the chosen server"
    )
    assert manager.status()["server"] == "two", (
        "Selection must change the connected server"
    )
    await manager.close()


async def test_select_key_reports_an_unknown_server(manager: TunnelManager) -> None:
    """Display a useful error for a server absent from the configuration."""
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["select"]("missing")
    await until(lambda: panel.last_action is not None)
    assert panel.last_action.startswith("select: Unknown server: missing"), (
        "Unknown selection must name the missing server"
    )
    await manager.close()


async def test_key_actions_run_and_feed_the_panel(manager: TunnelManager) -> None:
    """Every key reports its outcome, whatever the tunnel does."""
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    expected = [
        ("reload", "reload: no configuration file was given to the service", ()),
        ("rotate", "rotate: one", ()),
        ("restart", "restart: one", ()),
        ("disconnect", "disconnect: tunnel stopped", ()),
        ("select", "select: two", ("two",)),
    ]
    for name, message, args in expected:
        panel.last_action = None
        actions[name](*args)
        await until(lambda: panel.last_action is not None)
        assert panel.last_action == message, (
            f"Action {name!r} must report {message!r}, got {panel.last_action!r}"
        )
    await manager.close()


async def test_connection_key_adopts_a_pending_bind(
    yaml_file: Path, manager: TunnelManager
) -> None:
    """A key that creates ssh carries the deferred SOCKS bind with it."""
    reloader = ConfigReloader(yaml_file, manager.config, targets=[manager])
    yaml_file.write_text(document(proxy={"ip": "127.0.0.1", "port": 1090}))
    await reloader.reload_now()
    assert manager.config.proxy.port == 1080, (
        "Reload must defer the SOCKS port until the connection key runs"
    )
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=reloader)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    actions["rotate"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "rotate: one", (
        "Connection key must report the new connection"
    )
    assert manager.config.proxy.port == 1090, (
        "Connection key must adopt the pending SOCKS port"
    )
    assert reloader.pending is None, (
        "Connection key must clear the adopted pending configuration"
    )
    await manager.close()


async def test_reload_key_reports_an_unchanged_file(
    yaml_file: Path, manager: TunnelManager
) -> None:
    """Report unchanged configuration through the reload keyboard action."""
    reloader = ConfigReloader(yaml_file, manager.config, targets=[manager])
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=reloader)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    actions["reload"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "reload: unchanged", (
        "Reload key must report unchanged content"
    )
    await manager.close()


async def test_key_failure_is_shown_in_the_panel(manager: TunnelManager) -> None:
    """Convert unexpected keyboard action failures into panel feedback."""

    async def exploding_disconnect() -> None:
        message = "tunnel exploded"
        raise RuntimeError(message)

    manager.disconnect = exploding_disconnect
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    actions["disconnect"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "disconnect: tunnel exploded", (
        "Unexpected key failures must appear in feedback"
    )
    await manager.close()


def test_key_without_a_loop_is_dropped() -> None:
    """Drop keyboard actions safely before application lifespan starts."""
    manager = TunnelManager(config())
    panel = PanelState()
    application = SimpleNamespace(state=SimpleNamespace(loop=None, reloader=None))
    actions = key_actions(application, manager, panel)
    # Before the lifespan runs there is nothing to schedule a coroutine on.
    actions["disconnect"]()
    assert panel.last_action is None, (
        "Key without an event loop must not schedule feedback"
    )


def test_reload_feedback_updates_the_panel_and_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh logging only for successful reloads while reporting failures."""
    calls = []
    monkeypatch.setattr(
        "tunnel_switch.cli.configure_logging",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Path("/tmp/x.log"),
    )
    panel = SimpleNamespace(last_reload=None)
    reloaded = _reload_feedback(panel)
    reloaded(ReloadOutcome(applied=True, config=config()))
    assert panel.last_reload.endswith("applied (2 servers)"), (
        "Reload feedback must show the applied server count"
    )
    assert len(calls) == 1, "Applied reload must configure logging once"
    # A failed attempt is reported to the panel but never rebuilds handlers.
    reloaded(ReloadOutcome(error="bad: not yaml"))
    assert panel.last_reload.endswith("failed: bad: not yaml"), (
        "Failed reload must update panel feedback"
    )
    assert len(calls) == 1, "Failed reload must not rebuild logging handlers"


def test_cli_overrides_outlive_a_reload() -> None:
    """Preserve supplied CLI overrides while letting unsupplied YAML values through."""
    cfg = config()
    cfg.state_file = Path("/tmp/cli-state.json")
    prepare = _cli_overrides(cfg, Path("--state-file"), "one")
    desired = config(state_file="yaml-state.json")
    prepare(desired)
    assert desired.state_file == Path("/tmp/cli-state.json"), (
        "CLI state-file override must win after reload"
    )
    assert desired.server_name == "one", "CLI server override must win after reload"
    # Without the switches the YAML value passes through untouched.
    untouched = config(state_file="yaml-state.json")
    _cli_overrides(cfg, None, None)(untouched)
    assert untouched.state_file == Path("yaml-state.json"), (
        "Absent CLI state override must preserve YAML"
    )
    assert untouched.server_name is None, (
        "Absent CLI server override must preserve the default"
    )


def test_create_app_refuses_an_empty_token(manager: TunnelManager) -> None:
    """Reject API creation with an empty authentication token."""
    with pytest.raises(ValueError, match="must not be empty"):
        create_app(manager.config, "")


async def test_rotate_endpoint_adopts_a_deferred_bind(
    yaml_file: Path, manager: TunnelManager
) -> None:
    """Adopt a pending SOCKS port when an authenticated rotation is admitted."""
    app = create_app(manager.config, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yaml_file.write_text(document(proxy={"ip": "127.0.0.1", "port": 1090}))
        reloaded = (await client.post("/reload", headers=headers)).json()
        assert reloaded["applied"], "Bind reload must report applied hot fields"
        assert reloaded["deferred"], "Bind reload must report deferred changes"
        assert manager.config.proxy.port == 1080, (
            "Reload alone must retain the current SOCKS bind"
        )
        status = (await client.post("/rotate", headers=headers)).json()
    assert status["proxy"]["port"] == 1090, (
        "Rotation response must report the adopted SOCKS bind"
    )
    await manager.close()


async def test_rejected_rotation_keeps_the_running_bind(
    yaml_file: Path, manager: TunnelManager
) -> None:
    """A 429 must not adopt the deferred bind of the tunnel still serving."""
    app = create_app(manager.config, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yaml_file.write_text(
            document(cooldown_seconds=99, proxy={"ip": "127.0.0.1", "port": 1090})
        )
        assert (await client.post("/reload", headers=headers)).json()["applied"], (
            "First bind reload must apply"
        )
        status = (await client.post("/rotate", headers=headers)).json()
        assert status["proxy"]["port"] == 1090, (
            "First admitted rotation must adopt the first pending port"
        )
        # While the cooldown holds, another deferred bind arrives and the next
        # rotation is rejected before it ever admits.
        yaml_file.write_text(
            document(cooldown_seconds=99, proxy={"ip": "127.0.0.1", "port": 1100})
        )
        reloaded = (await client.post("/reload", headers=headers)).json()
        assert reloaded["deferred"], "Second SOCKS bind change must remain deferred"
        rejected = await client.post("/rotate", headers=headers)
        assert rejected.status_code == 429, "Cooldown must reject the second rotation"
        # The running tunnel keeps serving on the bind it was started with.
        assert manager.config.proxy.port == 1090, (
            "Rejected rotation must preserve the serving port"
        )
        assert app.state.reloader.pending is not None, (
            "Rejected rotation must retain pending configuration"
        )
    await manager.close()


async def test_reload_endpoint_429_when_a_rotation_starts_mid_read(
    yaml_file: Path,
    manager: TunnelManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry gate cannot see a rotation that starts during the file read."""
    real_load = reload_module.load_config

    def load_while_rotating(path: Path) -> Config:
        manager._busy = True  # A rotation is admitted while the read runs.
        return real_load(path)

    monkeypatch.setattr(reload_module, "load_config", load_while_rotating)
    app = create_app(manager.config, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yaml_file.write_text(document(cooldown_seconds=99))
        response = await client.post("/reload", headers=headers)
    assert response.status_code == 429, (
        "Rotation admitted mid-read must refuse the reload"
    )
    assert response.headers["Retry-After"] == "1", (
        "Mid-read refusal must provide the retry delay"
    )
    # A refused reload applies nothing, so the running configuration stands.
    assert manager.config.cooldown_seconds == 0, (
        "Mid-read refusal must preserve the running cooldown"
    )
    manager._busy = False
    await manager.close()


async def test_signal_trigger_failure_keeps_the_poller_alive(
    yaml_file: Path, manager: TunnelManager
) -> None:
    """The SIGHUP hook is best effort: a loop that refuses it changes nothing."""
    reloader = ConfigReloader(yaml_file, manager.config, targets=[manager])
    loop = SimpleNamespace(
        add_signal_handler=lambda *args, **kwargs: (_ for _ in ()).throw(
            NotImplementedError("no signals here")
        )
    )
    stop = asyncio.Event()
    app = SimpleNamespace(state=SimpleNamespace(reloader=reloader))
    poller = api_module._start_reload(app, loop, stop, True)
    assert poller is not None, (
        "Unsupported signal hook must not prevent poller creation"
    )
    stop.set()
    await poller
    assert stop.is_set(), "Poller shutdown must preserve the stop event"


async def test_poller_idles_then_stops_without_changes(
    yaml_file: Path, manager: TunnelManager
) -> None:
    """Keep unchanged polling alive until an explicit stop without applying reloads."""
    reloader = ConfigReloader(
        yaml_file, manager.config, targets=[manager], interval=0.02
    )
    stop = asyncio.Event()
    task = asyncio.create_task(reloader.run(stop))
    await asyncio.sleep(0.1)  # several idle ticks: signature unchanged
    assert not task.done(), "Idle poller must remain alive before stop"
    stop.set()
    await asyncio.wait_for(task, 2)
    assert reloader.last_reload is None, "Idle ticks must not record applied reloads"
    await manager.close()


def test_signature_of_a_missing_file_is_none(tmp_path: Path) -> None:
    """Treat an absent configuration file as having no poll signature."""
    reloader = ConfigReloader(tmp_path / "gone.yaml", config())
    assert reloader._signature_now() is None, "Missing file must have no signature"


def test_apply_pending_binds_without_a_pending_change_is_a_noop(tmp_path: Path) -> None:
    """Return no adopted configuration when no binds are pending."""
    reloader = ConfigReloader(tmp_path / "config.yaml", config())
    assert reloader.apply_pending_binds() is None, (
        "Adoption without pending binds must be a no-op"
    )


def test_reload_outcome_summaries() -> None:
    """Summarize failed, unchanged, deferred, and applied reload outcomes."""
    assert ReloadOutcome(error="bad: not yaml").summary() == "failed: bad: not yaml", (
        "Failure summary must include the error"
    )
    assert ReloadOutcome(unchanged=True).summary() == "unchanged", (
        "Unchanged outcome must have its own summary"
    )
    applied = ReloadOutcome(
        applied=True,
        deferred=[
            "proxy bind 127.0.0.1:1080 -> 127.0.0.1:1090 applies on the next rotation"
        ],
        config=config(),
    )
    assert applied.summary() == (
        "applied (2 servers); proxy bind 127.0.0.1:1080 -> 127.0.0.1:1090"
        " applies on the next rotation"
    ), "Applied summary must include server count and full deferred bind detail"
    assert ReloadOutcome(applied=True, config=config()).summary() == (
        "applied (2 servers)"
    ), "Applied summary must count configured servers"
    assert ReloadOutcome(applied=True).summary() == "applied (0 servers)", (
        "Applied outcome without config must report zero servers"
    )


def test_payload_reports_every_field() -> None:
    """Serialize every API payload field for unchanged and refused outcomes."""
    payload = ReloadOutcome(unchanged=True, config=config()).payload()
    assert payload == {
        "applied": False,
        "unchanged": True,
        "refused": False,
        "error": None,
        "deferred": [],
        "servers": 2,
    }, "Unchanged payload must match every documented field"
    assert ReloadOutcome(refused=True, error="rotation in progress").payload() == {
        "applied": False,
        "unchanged": False,
        "refused": True,
        "error": "rotation in progress",
        "deferred": [],
        "servers": None,
    }, "Refused payload must match every documented field"


def test_refused_summary_is_not_reported_as_a_failure() -> None:
    """Distinguish busy refusals from invalid-configuration failures."""
    outcome = ReloadOutcome(refused=True, error="rotation in progress")
    assert outcome.summary() == "refused: rotation in progress", (
        "Refusal summary must retain the busy reason"
    )
    assert ReloadOutcome(refused=True).summary() == "refused", (
        "Refusal without a reason must not claim failure"
    )


@pytest.mark.parametrize(
    "started,expected",
    [
        (None, ""),
        ("", ""),
        ("seconds", "30s"),
        ("minutes", "5m 30s"),
        ("hours", "2h 5m"),
    ],
)
def test_elapsed_formats_every_span(started: str | None, expected: str) -> None:
    """Format absent, second, minute, and hour connection durations."""
    now = datetime.now(UTC)
    spans = {
        "seconds": timedelta(seconds=30),
        "minutes": timedelta(minutes=5, seconds=30),
        "hours": timedelta(hours=2, minutes=5),
    }
    value = None if started in (None, "") else (now - spans[started]).isoformat()
    assert _elapsed(value) == expected, (
        f"Duration category {started!r} must render as {expected!r}"
    )


def test_panel_row_of_a_disabled_log_file() -> None:
    """`file: null` is reported as disabled rather than as a path."""
    text = rendered(config(logging={"file": None}), state=None)
    assert "disabled" in text, "Null log file must render as disabled"


def test_key_reader_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return no keyboard descriptor for nonterminal standard input."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    descriptor, restore = _key_reader()
    assert descriptor is None, "Nonterminal input must not enable keyboard reads"
    restore()


def test_key_reader_on_a_non_posix_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable terminal key setup on unsupported operating systems."""
    monkeypatch.setattr(status.os, "name", "nt")
    descriptor, restore = status._key_reader()
    assert descriptor is None, "Non-POSIX platform must not enable keyboard reads"
    restore()


def test_key_reader_cbreaks_a_terminal_and_restores_it(
    monkeypatch: pytest.MonkeyPatch,
    terminal_modules: tuple[ModuleType, ModuleType],
) -> None:
    """Enter cbreak, disable flow control, and restore the saved terminal mode."""
    termios, tty = terminal_modules
    # The shape tcgetattr() really returns: index 0 holds the input flags.
    saved = [
        termios.ICRNL | termios.IXON | termios.IXANY | termios.IXOFF,
        0,
        0,
        termios.ICANON | termios.ECHO,
        0,
        0,
        [0] * 32,
    ]
    applied = []
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: list(saved))
    monkeypatch.setattr(tty, "setcbreak", lambda fd: applied.append(("cbreak", fd)))
    monkeypatch.setattr(
        termios,
        "tcsetattr",
        lambda fd, when, buf: applied.append(("tcsetattr", when, list(buf))),
    )
    descriptor, restore = _key_reader()
    assert descriptor == 7, "Terminal key reader must return stdin's descriptor"
    # cbreak first, then flow control off so Ctrl+S reaches the key map.
    assert [entry[0] for entry in applied] == ["cbreak", "tcsetattr"], (
        "Flow-control change must follow cbreak setup"
    )
    when, mode = applied[1][1], applied[1][2]
    assert when == termios.TCSANOW, "Flow-control change must apply immediately"
    assert mode[0] == termios.ICRNL, (
        "Flow-control flags must clear without changing ICRNL"
    )
    assert mode[3] == saved[3], "Flow-control setup must preserve local mode flags"
    restore()
    assert applied[-1] == ("tcsetattr", termios.TCSADRAIN, saved), (
        "Restore must drain output and restore the complete saved mode"
    )


def test_key_reader_keeps_the_keys_when_flow_control_cannot_be_dropped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    terminal_modules: tuple[ModuleType, ModuleType],
) -> None:
    """Keep other keyboard shortcuts and warn when flow control cannot change."""
    termios, tty = terminal_modules
    mode = [termios.IXON, 0, 0, 0, 0, 0, [0] * 32]
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: list(mode))
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, buf: (_ for _ in ()).throw(OSError())
    )
    with caplog.at_level(logging.WARNING, logger="tunnel_switch.status"):
        descriptor, restore = _key_reader()
    # A terminal that refuses the change keeps Ctrl+S as XOFF, but every other
    # key still works, so the panel does not lose its keyboard over it.
    assert descriptor == 7, (
        "Flow-control refusal must not disable the keyboard descriptor"
    )
    # The refusal is reported: a Ctrl+S that only pauses the output would
    # otherwise look exactly like a key the panel never received.
    assert "Ctrl+S" in caplog.text, (
        "Flow-control refusal must warn about the unavailable Ctrl+S shortcut"
    )
    restore()


def test_key_reader_reads_only_the_flow_control_flags_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    terminal_modules: tuple[ModuleType, ModuleType],
) -> None:
    """Survive missing termios flow-control flags while warning about Ctrl+S."""
    termios, tty = terminal_modules
    # A build of termios without these flags must not raise: this runs after
    # cbreak is on and would otherwise strand the terminal with no restore.
    for flag in ("IXON", "IXANY", "IXOFF"):
        monkeypatch.delattr(termios, flag)
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: [0, 0, 0, 0, 0, 0, [0] * 32])
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, buf: None)
    with caplog.at_level(logging.WARNING, logger="tunnel_switch.status"):
        descriptor, restore = _key_reader()
    assert descriptor == 7, (
        "Missing termios flags must not disable the keyboard descriptor"
    )
    assert "Ctrl+S" in caplog.text, "Missing flow-control flags must warn about Ctrl+S"
    restore()


def test_key_reader_gives_up_when_the_terminal_refuses(
    monkeypatch: pytest.MonkeyPatch,
    terminal_modules: tuple[ModuleType, ModuleType],
) -> None:
    """Disable keyboard reading safely when terminal mode inspection fails."""
    termios, _tty = terminal_modules
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(
        termios, "tcgetattr", lambda fd: (_ for _ in ()).throw(OSError())
    )
    descriptor, restore = _key_reader()
    assert descriptor is None, (
        "Refused terminal inspection must disable keyboard reading"
    )
    restore()


def test_key_reader_survives_a_failing_restore(
    monkeypatch: pytest.MonkeyPatch,
    terminal_modules: tuple[ModuleType, ModuleType],
) -> None:
    """Keep terminal restoration failures from crashing the service."""
    termios, tty = terminal_modules
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    # The shape tcgetattr() really returns, so the flow-control step runs too.
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: [0, 0, 0, 0, 0, 0, [0] * 32])
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, buf: (_ for _ in ()).throw(OSError())
    )
    descriptor, restore = _key_reader()
    assert descriptor == 7, (
        "Keyboard setup must survive flow-control failures before restore"
    )
    restore()  # a terminal that refuses to be restored ends the display, not the service


def test_handle_keys_dispatches_and_ignores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch mapped actions and silently ignore unmapped or absent handlers."""
    calls = []
    state = PanelState()
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x12")
    status._handle_keys(0, {"reload": lambda: calls.append("reload")}, state)
    # An action the mapping does not know is dropped without a word.
    status._handle_keys(0, {"rotate": lambda: calls.append("rotate")}, state)
    status._handle_keys(0, None, state)
    assert calls == ["reload"], "Only the mapped reload action must dispatch"
    assert state.last_action is None, "Ignored key actions must not create feedback"


def test_handle_keys_waits_for_the_escape_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combine a separately read Escape prefix and Alt+R follow-up."""
    reads = iter([b"\x1b", b"r"])
    monkeypatch.setattr(status.os, "read", lambda fd, n: next(reads))
    monkeypatch.setattr(status.select, "select", lambda *args: ([0], [], []))
    calls = []
    status._handle_keys(0, {"restart": lambda: calls.append("restart")}, PanelState())
    assert calls == ["restart"], (
        "Split Alt+R sequence must dispatch restart exactly once"
    )


def test_handle_keys_reports_a_failing_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report synchronous action failures in the panel instead of raising."""

    def refuse() -> None:
        message = "no keyboard"
        raise OSError(message)

    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x05")
    state = PanelState()
    status._handle_keys(0, {"connect": refuse}, state)
    assert state.last_action == "connect: no keyboard", (
        "Failed action must report its name and error"
    )


def test_handle_keys_opens_moves_and_confirms_the_picker(
    monkeypatch: pytest.MonkeyPatch, manager: TunnelManager
) -> None:
    """Open with Alt+S, move down, and connect only after Enter confirms."""
    picked = []
    actions = {"select": lambda name: picked.append(name)}
    state = PanelState()
    reads = iter([b"\x1bs", b"\x1b[B", b"\r"])  # Alt+S, arrow down, Enter
    monkeypatch.setattr(status.os, "read", lambda fd, n: next(reads))
    status._handle_keys(0, actions, state, manager)
    assert state.picker_index == 0, (
        "Alt+S must open on the first server without connecting"
    )
    status._handle_keys(0, actions, state, manager)
    assert state.picker_index == 1, "Down arrow must advance the picker highlight"
    status._handle_keys(0, actions, state, manager)
    assert state.picker_index is None, "Enter must close the picker"
    assert picked == ["two"], "Enter must select only the highlighted second server"


def test_handle_keys_opens_the_picker_from_ctrl_s(
    monkeypatch: pytest.MonkeyPatch, manager: TunnelManager
) -> None:
    """Open the picker with Ctrl+S and confirm its initial highlight."""
    picked = []
    actions = {"select": lambda name: picked.append(name)}
    state = PanelState()
    reads = iter([b"\x13", b"\r"])  # Ctrl+S opens it, Enter takes the highlight
    monkeypatch.setattr(status.os, "read", lambda fd, n: next(reads))
    status._handle_keys(0, actions, state, manager)
    assert state.picker_index == 0, "Ctrl+S must open on the first server"
    status._handle_keys(0, actions, state, manager)
    assert state.picker_index is None, "Ctrl+S picker must close after Enter"
    assert picked == ["one"], "Enter must select the initial server exactly once"


async def test_handle_keys_opens_the_picker_on_the_connected_server(
    monkeypatch: pytest.MonkeyPatch,
    manager: TunnelManager,
) -> None:
    """Initialize the picker highlight to the currently connected server."""
    await manager.rotate()
    await manager.rotate()
    state = PanelState()
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x1bs")
    status._handle_keys(0, {"select": lambda _name: None}, state, manager)
    assert state.picker_index == 1, "Picker must highlight the connected second server"
    await manager.close()


def test_handle_keys_picker_owns_the_keyboard(
    monkeypatch: pytest.MonkeyPatch, manager: TunnelManager
) -> None:
    """Suppress normal actions while picking and cancel without connecting."""
    state = PanelState()
    state.picker_index = 0
    rotated, picked = [], []
    # While the picker is open, a rotation key is ignored rather than acted on.
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x0e")
    status._handle_keys(0, {"rotate": lambda: rotated.append(1)}, state, manager)
    assert rotated == [], "Picker must suppress rotation shortcuts"
    assert state.picker_index == 0, (
        "Suppressed rotation must leave the picker highlight unchanged"
    )
    # Esc closes the picker without connecting anywhere.
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x1b")
    monkeypatch.setattr(status.select, "select", lambda *args: ([], [], []))
    status._handle_keys(0, {"select": picked.append}, state, manager)
    assert state.picker_index is None, "Escape must close the picker"
    assert picked == [], "Escape must not connect to a server"


def test_handle_keys_ignores_picker_bytes_it_does_not_know(
    monkeypatch: pytest.MonkeyPatch, manager: TunnelManager
) -> None:
    """Leave the open picker unchanged for unsupported input bytes."""
    state = PanelState()
    state.picker_index = 0
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"z")
    status._handle_keys(0, {"select": lambda _name: None}, state, manager)
    assert state.picker_index == 0, (
        "Unknown picker byte must leave the first server highlighted"
    )


def test_handle_keys_never_opens_a_picker_it_could_not_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to open a picker without a manager providing its server list."""
    # Without a manager there is no server list to offer or confirm from.
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x1bs")
    state = PanelState()
    status._handle_keys(0, {"select": lambda _name: None}, state)
    assert state.picker_index is None, "Missing manager must prevent picker opening"


def test_handle_keys_closes_a_picker_whose_highlight_went_stale(
    monkeypatch: pytest.MonkeyPatch, manager: TunnelManager
) -> None:
    """Close a stale picker highlight without selecting a nonexistent server."""
    # A reload shrank the list to two servers while the picker was open.
    state = PanelState()
    state.picker_index = 5
    picked = []
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\r")
    status._handle_keys(0, {"select": picked.append}, state, manager)
    assert state.picker_index is None, (
        "Stale highlight confirmation must close the picker"
    )
    assert picked == [], "Stale highlight must not select any server"


def test_display_reads_keys_until_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive branch: select wakes on a key, the handler runs it."""
    manager = TunnelManager(config())
    stop = status.Event()
    calls = []
    rounds = []

    def fake_select(
        readers: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        if not rounds:
            rounds.append(1)
            return (readers, [], [])
        stop.set()
        return ([], [], [])

    monkeypatch.setattr(status, "_key_reader", lambda: (7, lambda: None))
    monkeypatch.setattr(status.select, "select", fake_select)
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x12")
    status.run_status_display(
        manager,
        stop,
        console=Console(file=StringIO(), width=100),
        actions={"reload": lambda: calls.append("reload")},
        state=PanelState(),
    )
    assert calls == ["reload"], (
        "Interactive display must dispatch the readable reload key exactly once"
    )
    assert stop.is_set(), "Interactive display must exit when its stop event is set"
