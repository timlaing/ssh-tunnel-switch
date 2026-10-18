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
    assert outcome.applied and outcome.error is None
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
    assert outcome.error and not outcome.applied
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
    assert outcome.unchanged and not outcome.applied
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
    assert outcome.error == "rotation in progress"
    assert manager.config.cooldown_seconds == 0
    # A refused reload never reaches the panel or the logging hook.
    assert not reached
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
        assert payload["applied"] and payload["servers"] == 1
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
