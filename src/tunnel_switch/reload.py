"""Live configuration reload: apply YAML edits without dropping the SSH tunnel.

A reload validates the whole file first and then swaps one effective ``Config``
reference, so the API, the status display and the HTTP proxy either all see the
old configuration or all see the new one. Changes that are baked into a bound
socket (the running ``ssh -D``, the API listener and the HTTP proxy listener)
are carried forward instead, named in the outcome, and applied when the socket
is next recreated.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .config import Config, load_config

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
DEBOUNCE_SECONDS = 0.2


def _endpoint(listener) -> str:
    address = str(listener.ip)
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{listener.port}"


def merge_binds(current: Config, desired: Config) -> tuple[Config, list[str]]:
    """Return the effective config and the socket changes it cannot apply.

    ``desired`` carries every hot change (servers, timeouts, token, logging and
    the proxy limits), while the bound addresses keep their running values so
    ``/status``, the HTTP bridge and the next ``ssh -D`` all describe sockets
    that actually exist.
    """
    merged = desired.model_copy(deep=True)
    deferred = []
    if merged.proxy != current.proxy:
        deferred.append(
            f"proxy bind {_endpoint(current.proxy)} -> {_endpoint(merged.proxy)}"
            " applies on the next rotation"
        )
        merged.proxy = current.proxy
    if (merged.api.ip, merged.api.port) != (current.api.ip, current.api.port):
        deferred.append(
            f"API bind {_endpoint(current.api)} -> {_endpoint(merged.api)}"
            " requires a service restart"
        )
        merged.api.ip, merged.api.port = current.api.ip, current.api.port
    if (merged.http_proxy.enabled, merged.http_proxy.ip, merged.http_proxy.port) != (
        current.http_proxy.enabled,
        current.http_proxy.ip,
        current.http_proxy.port,
    ):
        deferred.append(
            f"HTTP proxy bind {_endpoint(current.http_proxy)}"
            f" -> {_endpoint(merged.http_proxy)} requires a service restart"
        )
        merged.http_proxy.enabled = current.http_proxy.enabled
        merged.http_proxy.ip, merged.http_proxy.port = (
            current.http_proxy.ip,
            current.http_proxy.port,
        )
    return merged, deferred


class ReloadOutcome:
    """One reload attempt, reported to callers, the log and the status panel."""

    def __init__(
        self,
        *,
        applied: bool = False,
        unchanged: bool = False,
        error: str | None = None,
        deferred: Sequence[str] = (),
        config: Config | None = None,
    ):
        self.applied = applied
        self.unchanged = unchanged
        self.error = error
        self.deferred = tuple(deferred)
        self.config = config

    def summary(self) -> str:
        if self.error is not None:
            return f"failed: {self.error}"
        if self.unchanged:
            return "unchanged"
        note = f"; {'; '.join(self.deferred)}" if self.deferred else ""
        servers = len(self.config.servers) if self.config is not None else 0
        return f"applied ({servers} servers){note}"

    def payload(self) -> dict:
        """The JSON shape returned by ``POST /reload``."""
        return {
            "applied": self.applied,
            "unchanged": self.unchanged,
            "error": self.error,
            "deferred": list(self.deferred),
            "servers": len(self.config.servers) if self.config is not None else None,
        }


class ConfigReloader:
    """Apply configuration changes, either on a timer or on demand.

    ``targets`` are objects whose ``config`` attribute is replaced with the
    effective configuration; ``hooks`` receive every applied or failed outcome
    (they reconfigure logging and feed the status panel); ``prepare`` re-applies
    per-run overrides such as the CLI's ``--state-file`` onto each freshly
    loaded config. ``busy`` reports whether a rotation is in progress, in which
    case a reload is refused: the rotation must never observe a half-changed
    server list.
    """

    def __init__(
        self,
        path: Path,
        config: Config,
        *,
        targets: Sequence = (),
        hooks: Sequence[Callable[[ReloadOutcome], None]] = (),
        prepare: Callable[[Config], None] | None = None,
        busy: Callable[[], bool] | None = None,
        interval: float = POLL_SECONDS,
    ):
        self.path = path
        self.config = config
        self.targets = list(targets)
        self.hooks = tuple(hooks)
        self.prepare = prepare
        self.busy = busy
        self.interval = interval
        self.last_reload = None
        self.last_error = None
        self.deferred: list[str] = []
        self.pending: Config | None = None
        self._signature = self._signature_now()

    def _signature_now(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    async def run(self, stop: asyncio.Event):
        """Poll the file and reload on a change until ``stop`` is set."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), self.interval)
            except TimeoutError:
                pass
            else:
                return
            if self._signature_now() == self._signature:
                continue
            # Let an editor that writes in place finish before validating.
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._signature = self._signature_now()
            await self.reload_now()

    async def reload_now(self) -> ReloadOutcome:
        """Validate the file and apply every change that needs no rebind."""
        if self.busy is not None and self.busy():
            logger.debug("Configuration reload deferred: a rotation is in progress")
            return ReloadOutcome(error="rotation in progress", config=self.config)
        try:
            desired = await asyncio.to_thread(load_config, self.path)
        except Exception as exc:
            self.last_error = str(exc)
            self.last_reload = f"{self._stamp()} failed: {exc}"
            outcome = ReloadOutcome(error=str(exc), config=self.config)
            logger.warning(
                "Configuration reload failed; keeping the running configuration: %s",
                exc,
            )
            self._notify(outcome)
            return outcome
        if self.prepare is not None:
            self.prepare(desired)
        merged, deferred = merge_binds(self.config, desired)
        self._signature = self._signature_now()
        if merged == self.config and not deferred:
            logger.debug("Configuration reloaded with no effective change")
            return ReloadOutcome(unchanged=True, deferred=deferred, config=self.config)
        self.config = merged
        self.deferred = deferred
        self.last_error = None
        # A SOCKS change can only be honoured when ssh binds afresh.
        self.pending = (
            desired
            if any(entry.startswith("proxy bind") for entry in deferred)
            else None
        )
        for target in self.targets:
            target.config = merged
        self.last_reload = f"{self._stamp()} applied ({len(merged.servers)} servers)"
        for entry in deferred:
            logger.warning("Configuration change deferred: %s", entry)
        logger.info(
            "Configuration reloaded from %s (%d servers)",
            self.path,
            len(merged.servers),
        )
        outcome = ReloadOutcome(applied=True, deferred=deferred, config=merged)
        self._notify(outcome)
        return outcome

    def apply_pending_binds(self) -> Config | None:
        """Adopt a deferred SOCKS bind just before a new ssh process starts."""
        if self.pending is None or self.pending.proxy == self.config.proxy:
            return None
        adopted = self.config.model_copy(deep=True)
        adopted.proxy = self.pending.proxy
        self.config = adopted
        self.pending = None
        self.deferred = [
            entry for entry in self.deferred if not entry.startswith("proxy bind")
        ]
        for target in self.targets:
            target.config = adopted
        logger.info(
            "SOCKS proxy bind updated to %s for the next tunnel",
            _endpoint(adopted.proxy),
        )
        return adopted

    def _notify(self, outcome: ReloadOutcome) -> None:
        for hook in self.hooks:
            hook(outcome)

    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S")
