"""Live configuration reload, its API surface and the interactive key map."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tunnel_switch.api import create_app
from tunnel_switch.config import Config, load_config
from tunnel_switch.reload import ConfigReloader
from tunnel_switch.status import PanelState, decode_key, status_panel
from tunnel_switch.tunnel import TunnelManager


def document(**changes):
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


def config(**changes):
    return Config.model_validate(json.loads(document(**changes)))


@pytest.fixture
def yaml_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(document())
    return path


class Process:
    """A fake ssh subprocess that stays up until terminated."""

    returncode = None

    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def manager(monkeypatch):
    """A tunnel manager whose ssh process and SOCKS socket are faked."""
    from unittest.mock import AsyncMock

    manager = TunnelManager(config())
    monkeypatch.setattr(manager, "_ensure_socket_free", lambda: None)
    monkeypatch.setattr(manager, "_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=lambda *a, **kw: Process()),
    )
    return manager


async def until(predicate, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


async def test_reload_applies_hot_fields_and_defers_binds(yaml_file):
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
    assert outcome.applied
    assert outcome.error is None
    assert [server.name for server in manager.config.servers] == [
        "one",
        "two",
        "three",
    ]
    assert manager.config.cooldown_seconds == 45
    # The bound sockets keep their running values; nothing is rebound.
    assert manager.config.proxy.port == 1080
    assert manager.config.api.port == 8765
    assert manager.config.api.token == "secret"
    assert reloader.last_error is None
    assert any(entry.startswith("proxy bind") for entry in outcome.deferred)
    assert any(entry.startswith("API bind") for entry in outcome.deferred)
    assert reloader.pending is not None
    await manager.close()


async def test_reload_defers_an_http_proxy_bind_change(yaml_file):
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(document(http_proxy={"enabled": True, "port": 8118}))
    outcome = await reloader.reload_now()
    assert outcome.applied
    # The listener was never started for it, so enabling stays deferred and the
    # status keeps reporting the socket that is actually bound (none).
    assert manager.config.http_proxy.enabled is False
    assert any(entry.startswith("HTTP proxy bind") for entry in outcome.deferred)
    await manager.close()


async def test_reload_keeps_running_config_when_yaml_is_invalid(yaml_file):
    cfg = load_config(yaml_file)
    outcomes = []
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(
        yaml_file, cfg, targets=[manager], hooks=[outcomes.append]
    )
    yaml_file.write_text("servers: []\n")
    outcome = await reloader.reload_now()
    assert outcome.error
    assert not outcome.applied
    assert [server.name for server in manager.config.servers] == ["one", "two"]
    # The failure is reported to /status and the panel, never raised at startup.
    assert reloader.last_error == outcome.error
    assert outcomes == [outcome]
    await manager.close()


async def test_reload_with_unchanged_content_is_not_applied(yaml_file):
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(document())  # A rewrite with nothing to apply.
    outcome = await reloader.reload_now()
    assert outcome.unchanged
    assert not outcome.applied
    assert reloader.last_reload is None
    await manager.close()


async def test_pending_bind_is_adopted_by_the_next_connection_change(yaml_file):
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text(document(proxy={"ip": "127.0.0.1", "port": 1090}))
    await reloader.reload_now()
    assert manager.config.proxy.port == 1080
    adopted = reloader.apply_pending_binds()
    assert adopted.proxy.port == 1090
    # A rotation reads the adopted bind when it starts ssh.
    assert manager.config.proxy.port == 1090
    assert reloader.pending is None
    assert not any(entry.startswith("proxy bind") for entry in reloader.deferred)
    await manager.close()


async def test_reload_defers_while_a_rotation_is_in_progress(yaml_file):
    cfg = load_config(yaml_file)
    reached = []
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(
        yaml_file, cfg, targets=[manager], hooks=[reached.append], busy=lambda: True
    )
    yaml_file.write_text(document(cooldown_seconds=99))
    outcome = await reloader.reload_now()
    assert outcome.refused
    assert outcome.error == "rotation in progress"
    assert manager.config.cooldown_seconds == 0
    # A refused reload never reaches the panel or the logging hook.
    assert not reached
    await manager.close()


async def test_poller_retries_a_reload_refused_during_a_rotation(yaml_file):
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
        assert manager.config.cooldown_seconds == 0
        gate["busy"] = False  # The rotation finishes; the change must land.
        await until(lambda: manager.config.cooldown_seconds == 99)
    finally:
        stop.set()
        await task
    await manager.close()


async def test_reload_refuses_when_a_rotation_starts_during_validation(yaml_file):
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
    assert outcome.refused
    assert manager.config.cooldown_seconds == 0
    assert reloader.last_reload is None
    await manager.close()


async def test_unchanged_reload_clears_a_stale_error(yaml_file):
    """A file that validates again must not keep reporting the old failure."""
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    reloader = ConfigReloader(yaml_file, cfg, targets=[manager])
    yaml_file.write_text("servers: [")  # Broken mid-edit.
    failed = await reloader.reload_now()
    assert failed.error
    assert reloader.last_error is not None
    yaml_file.write_text(document())  # Repaired back to the running config.
    outcome = await reloader.reload_now()
    assert outcome.unchanged
    assert reloader.last_error is None
    await manager.close()


async def test_concurrent_reloads_run_one_at_a_time(yaml_file, manager, monkeypatch):
    """A queued reload waits for the running one, so neither applies a stale read."""
    import itertools
    from threading import Event

    from tunnel_switch import reload as reload_module

    real_load = reload_module.load_config
    entered, release, calls = Event(), Event(), []
    reads = itertools.count()

    def ordered_load(path):
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
        assert calls == []
        release.set()
        await asyncio.gather(first, second)
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    assert calls == [True, True]  # Both ran, one after the other.
    assert manager.config.cooldown_seconds == 99
    await manager.close()


async def test_poller_picks_up_a_changed_file(yaml_file):
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
    assert [server.name for server in manager.config.servers] == ["solo"]
    await manager.close()


async def test_reload_keeps_cli_overrides_for_the_life_of_the_process(yaml_file):
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
    assert outcome.applied
    assert manager.config.cooldown_seconds == 99
    # The CLI override wins for this run; the YAML value applies after a restart.
    assert manager.config.state_file == yaml_file.parent / "cli.json"
    await manager.close()


async def test_reload_endpoint_reports_and_applies(yaml_file):
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    app = create_app(cfg, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = (await client.get("/status", headers=headers)).json()
        assert status["config_reload"]["watching"] is True
        assert status["config_reload"]["path"] == str(yaml_file)
        assert status["config_reload"]["deferred"] == []
        yaml_file.write_text(
            document(
                servers=[{"name": "one", "host": "one.example", "user": "tunnel"}],
                cooldown_seconds=45,
            )
        )
        response = await client.post("/reload", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["applied"]
        assert payload["servers"] == 1
        assert payload["error"] is None
        status = (await client.get("/status", headers=headers)).json()
        assert status["config_reload"]["last_reload"]
        assert status["config_reload"]["last_error"] is None
    await manager.close()


async def test_reload_endpoint_is_guarded(yaml_file):
    cfg = load_config(yaml_file)
    manager = TunnelManager(cfg)
    app = create_app(cfg, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/reload")).status_code == 401
        manager._busy = True
        rejected = await client.post("/reload", headers=headers)
        assert rejected.status_code == 429
        assert rejected.headers["Retry-After"] == "1"
        manager._busy = False
    await manager.close()


async def test_reload_without_a_config_path_is_not_available(manager):
    app = create_app(manager.config, "secret", manager)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post("/reload", headers=headers)
        assert missing.status_code == 404
        status = (await client.get("/status", headers=headers)).json()
        assert status["config_reload"]["watching"] is False
        assert status["config_reload"]["path"] is None
    await manager.close()


async def test_reload_rotates_the_bearer_token(yaml_file):
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
        assert (await client.post("/reload", headers=old)).status_code == 200
        assert (await client.post("/reload", headers=old)).status_code == 401
        assert (await client.post("/reload", headers=new)).status_code == 200
        assert (await client.get("/status", headers=new)).status_code == 200
    await manager.close()


@pytest.mark.parametrize(
    "sequence,action",
    [
        (b"\x12", "reload"),  # Ctrl+R
        (b"\x1br", "restart"),  # Alt+R
        (b"\x0e", "rotate"),  # Ctrl+N
        (b"\x05", "connect"),  # Ctrl+E
        (b"\x04", "disconnect"),  # Ctrl+D
        (b"x\x12", "reload"),  # A key read may carry extra bytes.
    ],
)
def test_decode_key_maps_the_control_keys(sequence, action):
    assert decode_key(sequence) == action


@pytest.mark.parametrize(
    "sequence",
    [b"", b"\x03", b"\x1b", b"\x1b[C", b"\x1bx", b"plain text"],  # Ctrl+C stays SIGINT
)
def test_decode_key_ignores_everything_else(sequence):
    assert decode_key(sequence) is None


def test_status_panel_shows_feedback_and_the_key_footer():
    cfg = config()
    state = PanelState()
    state.last_reload = "12:00:01 applied (2 servers)"
    state.last_action = "rotate: two"
    with_keys = rendered(cfg, state=state, keys=True)
    assert "Last reload" in with_keys
    assert "12:00:01 applied (2 servers)" in with_keys
    assert "Last action" in with_keys
    assert "rotate: two" in with_keys
    assert "Ctrl+R: reload config" in with_keys
    assert "Ctrl+N: rotate server" in with_keys
    without_keys = rendered(cfg, state=state)
    assert "Ctrl+R" not in without_keys
    # Without feedback the rows are omitted entirely.
    assert "Last reload" not in rendered(cfg)


def rendered(cfg, state=None, keys=False):
    from io import StringIO
    from rich.console import Console

    status = {
        "state": "disconnected",
        "connection_id": None,
        "started_at": None,
        "server": None,
        "proxy": {"ip": "127.0.0.1", "port": 1080},
        "last_error": None,
    }
    buffer = StringIO()
    Console(file=buffer, width=100).print(
        status_panel(cfg, status, state=state, keys=keys)
    )
    return buffer.getvalue()


async def test_connect_key_is_a_hint_when_already_connected(manager):
    from tunnel_switch.cli import key_actions

    await manager.rotate()
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    assert sorted(actions) == ["connect", "disconnect", "reload", "restart", "rotate"]
    actions["connect"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "already connected to one; Alt+R restarts it"
    await manager.close()


async def test_connect_key_reconnects_when_disconnected(manager):
    from tunnel_switch.cli import key_actions

    await manager.rotate()
    await manager.disconnect()
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["connect"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "connect: tunnel up on one"
    await manager.close()


async def test_disconnect_key_reports_a_rejected_connection_change(manager):
    from tunnel_switch.cli import key_actions

    manager._busy = True
    panel = PanelState()
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    actions = key_actions(application, manager, panel)
    actions["disconnect"]()
    await until(lambda: panel.last_action is not None)
    assert "in progress" in panel.last_action
    manager._busy = False
    await manager.close()


async def test_restart_key_reports_the_remaining_cooldown(manager):
    from tunnel_switch.cli import key_actions

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
    )
    await manager.close()


async def test_key_actions_run_and_feed_the_panel(manager):
    """Every key reports its outcome, whatever the tunnel does."""
    from tunnel_switch.cli import key_actions

    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    expected = [
        ("reload", "reload: no configuration file was given to the service"),
        ("rotate", "rotate: one"),
        ("restart", "restart: one"),
        ("disconnect", "disconnect: tunnel stopped"),
    ]
    for name, message in expected:
        panel.last_action = None
        actions[name]()
        await until(lambda: panel.last_action is not None)
        assert panel.last_action == message
    await manager.close()


async def test_connection_key_adopts_a_pending_bind(yaml_file, manager):
    """A key that creates ssh carries the deferred SOCKS bind with it."""
    from tunnel_switch.cli import key_actions

    reloader = ConfigReloader(yaml_file, manager.config, targets=[manager])
    yaml_file.write_text(document(proxy={"ip": "127.0.0.1", "port": 1090}))
    await reloader.reload_now()
    assert manager.config.proxy.port == 1080
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=reloader)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    actions["rotate"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "rotate: one"
    assert manager.config.proxy.port == 1090
    assert reloader.pending is None
    await manager.close()


async def test_reload_key_reports_an_unchanged_file(yaml_file, manager):
    from tunnel_switch.cli import key_actions

    reloader = ConfigReloader(yaml_file, manager.config, targets=[manager])
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=reloader)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    actions["reload"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "reload: unchanged"
    await manager.close()


async def test_key_failure_is_shown_in_the_panel(manager):
    from tunnel_switch.cli import key_actions

    async def exploding_disconnect():
        raise RuntimeError("tunnel exploded")

    manager.disconnect = exploding_disconnect
    application = SimpleNamespace(
        state=SimpleNamespace(loop=asyncio.get_running_loop(), reloader=None)
    )
    panel = PanelState()
    actions = key_actions(application, manager, panel)
    actions["disconnect"]()
    await until(lambda: panel.last_action is not None)
    assert panel.last_action == "disconnect: tunnel exploded"
    await manager.close()


def test_key_without_a_loop_is_dropped():
    from tunnel_switch.cli import key_actions

    manager = TunnelManager(config())
    panel = PanelState()
    application = SimpleNamespace(state=SimpleNamespace(loop=None, reloader=None))
    actions = key_actions(application, manager, panel)
    # Before the lifespan runs there is nothing to schedule a coroutine on.
    actions["disconnect"]()
    assert panel.last_action is None


def test_reload_feedback_updates_the_panel_and_logging(monkeypatch):
    from pathlib import Path

    from tunnel_switch.cli import _reload_feedback
    from tunnel_switch.reload import ReloadOutcome

    calls = []
    monkeypatch.setattr(
        "tunnel_switch.cli.configure_logging",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Path("/tmp/x.log"),
    )
    panel = SimpleNamespace(last_reload=None)
    reloaded = _reload_feedback(panel)
    reloaded(ReloadOutcome(applied=True, config=config()))
    assert panel.last_reload.endswith("applied (2 servers)")
    assert len(calls) == 1
    # A failed attempt is reported to the panel but never rebuilds handlers.
    reloaded(ReloadOutcome(error="bad: not yaml"))
    assert panel.last_reload.endswith("failed: bad: not yaml")
    assert len(calls) == 1


def test_cli_overrides_outlive_a_reload():
    from pathlib import Path

    from tunnel_switch.cli import _cli_overrides

    cfg = config()
    cfg.state_file = Path("/tmp/cli-state.json")
    prepare = _cli_overrides(cfg, Path("--state-file"), "one")
    desired = config(state_file="yaml-state.json")
    prepare(desired)
    assert desired.state_file == Path("/tmp/cli-state.json")
    assert desired.server_name == "one"
    # Without the switches the YAML value passes through untouched.
    untouched = config(state_file="yaml-state.json")
    _cli_overrides(cfg, None, None)(untouched)
    assert untouched.state_file == Path("yaml-state.json")
    assert untouched.server_name is None


def test_create_app_refuses_an_empty_token(manager):
    with pytest.raises(ValueError, match="must not be empty"):
        create_app(manager.config, "")


async def test_rotate_endpoint_adopts_a_deferred_bind(yaml_file, manager):
    app = create_app(manager.config, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yaml_file.write_text(document(proxy={"ip": "127.0.0.1", "port": 1090}))
        reloaded = (await client.post("/reload", headers=headers)).json()
        assert reloaded["applied"]
        assert reloaded["deferred"]
        assert manager.config.proxy.port == 1080
        status = (await client.post("/rotate", headers=headers)).json()
    assert status["proxy"]["port"] == 1090
    await manager.close()


async def test_rejected_rotation_keeps_the_running_bind(yaml_file, manager):
    """A 429 must not adopt the deferred bind of the tunnel still serving."""
    app = create_app(manager.config, "secret", manager, config_path=yaml_file)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yaml_file.write_text(
            document(cooldown_seconds=99, proxy={"ip": "127.0.0.1", "port": 1090})
        )
        assert (await client.post("/reload", headers=headers)).json()["applied"]
        status = (await client.post("/rotate", headers=headers)).json()
        assert status["proxy"]["port"] == 1090
        # While the cooldown holds, another deferred bind arrives and the next
        # rotation is rejected before it ever admits.
        yaml_file.write_text(
            document(cooldown_seconds=99, proxy={"ip": "127.0.0.1", "port": 1100})
        )
        reloaded = (await client.post("/reload", headers=headers)).json()
        assert reloaded["deferred"]
        rejected = await client.post("/rotate", headers=headers)
        assert rejected.status_code == 429
        # The running tunnel keeps serving on the bind it was started with.
        assert manager.config.proxy.port == 1090
        assert app.state.reloader.pending is not None
    await manager.close()


async def test_reload_endpoint_429_when_a_rotation_starts_mid_read(
    yaml_file, manager, monkeypatch
):
    """The entry gate cannot see a rotation that starts during the file read."""
    from tunnel_switch import reload as reload_module

    real_load = reload_module.load_config

    def load_while_rotating(path):
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
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    # A refused reload applies nothing, so the running configuration stands.
    assert manager.config.cooldown_seconds == 0
    manager._busy = False
    await manager.close()


async def test_signal_trigger_failure_keeps_the_poller_alive(yaml_file, manager):
    """The SIGHUP hook is best effort: a loop that refuses it changes nothing."""
    from types import SimpleNamespace as Namespace

    from tunnel_switch.api import _start_reload

    reloader = ConfigReloader(yaml_file, manager.config, targets=[manager])
    loop = Namespace(
        add_signal_handler=lambda *args, **kwargs: (_ for _ in ()).throw(
            NotImplementedError("no signals here")
        )
    )
    stop = asyncio.Event()
    app = Namespace(state=Namespace(reloader=reloader))
    poller = _start_reload(app, loop, stop, True)
    assert poller is not None
    stop.set()
    await poller
    assert stop.is_set()


async def test_poller_idles_then_stops_without_changes(yaml_file, manager):
    reloader = ConfigReloader(
        yaml_file, manager.config, targets=[manager], interval=0.02
    )
    stop = asyncio.Event()
    task = asyncio.create_task(reloader.run(stop))
    await asyncio.sleep(0.1)  # several idle ticks: signature unchanged
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert reloader.last_reload is None
    await manager.close()


def test_signature_of_a_missing_file_is_none(tmp_path):
    reloader = ConfigReloader(tmp_path / "gone.yaml", config())
    assert reloader._signature_now() is None


def test_apply_pending_binds_without_a_pending_change_is_a_noop(tmp_path):
    reloader = ConfigReloader(tmp_path / "config.yaml", config())
    assert reloader.apply_pending_binds() is None


def test_reload_outcome_summaries():
    from tunnel_switch.reload import ReloadOutcome

    assert ReloadOutcome(error="bad: not yaml").summary() == "failed: bad: not yaml"
    assert ReloadOutcome(unchanged=True).summary() == "unchanged"
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
    )
    assert ReloadOutcome(applied=True, config=config()).summary() == (
        "applied (2 servers)"
    )
    assert ReloadOutcome(applied=True).summary() == "applied (0 servers)"


def test_payload_reports_every_field():
    from tunnel_switch.reload import ReloadOutcome

    payload = ReloadOutcome(unchanged=True, config=config()).payload()
    assert payload == {
        "applied": False,
        "unchanged": True,
        "refused": False,
        "error": None,
        "deferred": [],
        "servers": 2,
    }
    assert ReloadOutcome(refused=True, error="rotation in progress").payload() == {
        "applied": False,
        "unchanged": False,
        "refused": True,
        "error": "rotation in progress",
        "deferred": [],
        "servers": None,
    }


def test_refused_summary_is_not_reported_as_a_failure():
    from tunnel_switch.reload import ReloadOutcome

    outcome = ReloadOutcome(refused=True, error="rotation in progress")
    assert outcome.summary() == "refused: rotation in progress"
    assert ReloadOutcome(refused=True).summary() == "refused"


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
def test_elapsed_formats_every_span(started, expected):
    from datetime import datetime, timedelta, timezone

    from tunnel_switch.status import _elapsed

    now = datetime.now(timezone.utc)
    spans = {
        "seconds": timedelta(seconds=30),
        "minutes": timedelta(minutes=5, seconds=30),
        "hours": timedelta(hours=2, minutes=5),
    }
    value = None if started in (None, "") else (now - spans[started]).isoformat()
    assert _elapsed(value) == expected


def test_panel_row_of_a_disabled_log_file():
    """`file: null` is reported as disabled rather than as a path."""
    text = rendered(config(logging={"file": None}), state=None)
    assert "disabled" in text


def test_key_reader_without_a_terminal(monkeypatch):
    import sys

    from tunnel_switch.status import _key_reader

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    descriptor, restore = _key_reader()
    assert descriptor is None
    restore()


def test_key_reader_on_a_non_posix_platform(monkeypatch):
    from tunnel_switch import status

    monkeypatch.setattr(status.os, "name", "nt")
    descriptor, restore = status._key_reader()
    assert descriptor is None
    restore()


def test_key_reader_cbreaks_a_terminal_and_restores_it(monkeypatch):
    import sys
    import termios
    import tty

    from tunnel_switch.status import _key_reader

    saved = ["original"]
    applied = []
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: saved)
    monkeypatch.setattr(tty, "setcbreak", lambda fd: applied.append(("cbreak", fd)))
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, buf: applied.append(("restore", buf))
    )
    descriptor, restore = _key_reader()
    assert descriptor == 7
    assert applied == [("cbreak", 7)]
    restore()
    assert applied == [("cbreak", 7), ("restore", saved)]


def test_key_reader_gives_up_when_the_terminal_refuses(monkeypatch):
    import sys
    import termios

    from tunnel_switch.status import _key_reader

    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(
        termios, "tcgetattr", lambda fd: (_ for _ in ()).throw(OSError())
    )
    descriptor, restore = _key_reader()
    assert descriptor is None
    restore()


def test_key_reader_survives_a_failing_restore(monkeypatch):
    import sys
    import termios
    import tty

    from tunnel_switch.status import _key_reader

    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 7)
    )
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["original"])
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, buf: (_ for _ in ()).throw(OSError())
    )
    descriptor, restore = _key_reader()
    assert descriptor == 7
    restore()  # a terminal that refuses to be restored ends the display, not the service


def test_handle_keys_dispatches_and_ignores(monkeypatch):
    from tunnel_switch import status

    calls = []
    state = PanelState()
    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x12")
    status._handle_keys(0, {"reload": lambda: calls.append("reload")}, state)
    # An action the mapping does not know is dropped without a word.
    status._handle_keys(0, {"rotate": lambda: calls.append("rotate")}, state)
    status._handle_keys(0, None, state)
    assert calls == ["reload"]
    assert state.last_action is None


def test_handle_keys_waits_for_the_escape_follow_up(monkeypatch):
    from tunnel_switch import status

    reads = iter([b"\x1b", b"r"])
    monkeypatch.setattr(status.os, "read", lambda fd, n: next(reads))
    monkeypatch.setattr(status.select, "select", lambda *args: ([0], [], []))
    calls = []
    status._handle_keys(0, {"restart": lambda: calls.append("restart")}, PanelState())
    assert calls == ["restart"]


def test_handle_keys_reports_a_failing_action(monkeypatch):
    from tunnel_switch import status

    def refuse():
        raise OSError("no keyboard")

    monkeypatch.setattr(status.os, "read", lambda fd, n: b"\x05")
    state = PanelState()
    status._handle_keys(0, {"connect": refuse}, state)
    assert state.last_action == "connect: no keyboard"


def test_display_reads_keys_until_stop(monkeypatch):
    """The interactive branch: select wakes on a key, the handler runs it."""
    from io import StringIO

    from rich.console import Console

    from tunnel_switch import status

    manager = TunnelManager(config())
    stop = status.Event()
    calls = []
    rounds = []

    def fake_select(readers, writes, errors, timeout):
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
    assert calls == ["reload"]
    assert stop.is_set()
