from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from threading import Event, Thread, current_thread
from typing import Protocol

from nailong_agent.event_bus import EventBus
from nailong_agent.events import PetApplicationRule, PetPreferences, RawActivitySignal
from nailong_agent.health import NailongHealthMonitor
from nailong_agent.privacy import PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore


@dataclass(frozen=True)
class ForegroundWindow:
    process_id: int
    executable_name: str
    idle_seconds: float | None = None
    is_fullscreen: bool = False
    is_meeting_likely: bool = False
    window_title_hint: str | None = None
    ide_activity_hint: str | None = None


class ForegroundActivitySource(Protocol):
    def start(self, on_change: Callable[[ForegroundWindow], None]) -> None: ...

    def stop(self) -> None: ...

    def sample_current(self) -> ForegroundWindow | None: ...


@dataclass(frozen=True)
class IdleState:
    idle_seconds: float


class IdleStateSource(Protocol):
    def start(self, on_idle: Callable[[IdleState], None]) -> None: ...

    def stop(self) -> None: ...


class WindowActivityCollector:
    def __init__(
        self,
        *,
        source: ForegroundActivitySource,
        idle_source: IdleStateSource | None = None,
        privacy_policy: PrivacyPolicy,
        privacy_store: PrivacyStore,
        event_bus: EventBus,
        preferences: Callable[[], PetPreferences],
        application_rules: Callable[[], list[PetApplicationRule]],
        clock: Callable[[], float] = monotonic,
        on_error: Callable[[Exception], None] | None = None,
        health_monitor: NailongHealthMonitor | None = None,
        watchdog_interval_seconds: float = 15.0,
        reconnect_interval_seconds: float = 30.0,
    ) -> None:
        self.source = source
        self.idle_source = idle_source
        self.privacy_policy = privacy_policy
        self.privacy_store = privacy_store
        self.event_bus = event_bus
        self.preferences = preferences
        self.application_rules = application_rules
        self.clock = clock
        self.on_error = on_error
        self.health_monitor = health_monitor
        if watchdog_interval_seconds <= 0 or reconnect_interval_seconds <= 0:
            raise ValueError("watchdog intervals must be positive")
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.reconnect_interval_seconds = reconnect_interval_seconds
        self._started = False
        self._last_seen: dict[str, float] = {}
        self._last_foreground_category: str | None = None
        self._last_reconnect_at = float("-inf")
        self._watchdog_stop = Event()
        self._watchdog_thread: Thread | None = None

    def start(self) -> None:
        if self._started:
            return
        try:
            set_error_handler = getattr(self.source, "set_error_handler", None)
            if callable(set_error_handler):
                set_error_handler(self._on_source_error)
            self.source.start(self._on_foreground_change)
            if self.idle_source is not None:
                self.idle_source.start(self._on_idle_change)
            self._started = True
            if self.health_monitor is not None:
                self.health_monitor.record_listener(
                    running=not bool(getattr(self.source, "stopped", False))
                )
            self._sample_current()
            self._watchdog_stop.clear()
            self._watchdog_thread = Thread(
                target=self._run_watchdog,
                name="nailong-foreground-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
        except Exception:
            if self.health_monitor is not None:
                self.health_monitor.record_listener(running=False, error_code="listener_error")
            raise

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._watchdog_stop.set()
        if self._watchdog_thread is not None and self._watchdog_thread is not current_thread():
            self._watchdog_thread.join(2.0)
        self._watchdog_thread = None
        if self.idle_source is not None:
            self.idle_source.stop()
        self.source.stop()
        if self.health_monitor is not None:
            self.health_monitor.record_listener(running=False)

    def _run_watchdog(self) -> None:
        while not self._watchdog_stop.wait(self.watchdog_interval_seconds):
            try:
                self._watchdog_tick()
            except Exception:
                self._on_source_error(RuntimeError("activity source health check failed"))

    def _watchdog_tick(self) -> None:
        if not self._started:
            return
        if bool(getattr(self.source, "stopped", False)):
            now = self.clock()
            if now - self._last_reconnect_at < self.reconnect_interval_seconds:
                return
            self._last_reconnect_at = now
            self.source.start(self._on_foreground_change)
            if bool(getattr(self.source, "stopped", False)):
                self._on_source_error(RuntimeError("activity source restart failed"))
                return
            if self.health_monitor is not None:
                self.health_monitor.record_listener(running=True)
        self._sample_current()

    def _sample_current(self) -> None:
        sample_current = getattr(self.source, "sample_current", None)
        if not callable(sample_current):
            return
        try:
            window = sample_current()
        except Exception as exc:
            self._on_source_error(exc)
            return
        if window is None:
            return
        application_category = _normalize_application_id(window.executable_name)
        if application_category == self._last_foreground_category:
            return
        self._on_foreground_change(window)

    def _on_source_error(self, _: Exception) -> None:
        if self.health_monitor is not None:
            self.health_monitor.record_listener(running=False, error_code="listener_error")
        if self.on_error is not None:
            self.on_error(RuntimeError("activity source failed"))

    def _on_foreground_change(self, window: ForegroundWindow) -> None:
        try:
            self._collect(window)
        except Exception as exc:
            self.stop()
            if self.health_monitor is not None:
                self.health_monitor.record_listener(running=False, error_code="listener_error")
            if self.on_error is not None:
                self.on_error(exc)

    def _on_idle_change(self, state: IdleState) -> None:
        try:
            preferences = self.preferences()
            if not preferences.activity_listener_enabled or preferences.manual_pause_enabled:
                return
            signal = RawActivitySignal(
                source="idle",
                application_id="system",
                metadata={"idle_seconds": state.idle_seconds},
            )
            self._persist_and_publish(signal)
        except Exception as exc:
            self.stop()
            if self.health_monitor is not None:
                self.health_monitor.record_listener(running=False, error_code="listener_error")
            if self.on_error is not None:
                self.on_error(exc)

    def _collect(self, window: ForegroundWindow) -> None:
        preferences = self.preferences()
        if not preferences.activity_listener_enabled or preferences.manual_pause_enabled:
            return
        application_id = _normalize_application_id(window.executable_name)
        rules = {rule.application_id.casefold(): rule.rule for rule in self.application_rules()}
        if rules.get(application_id) == "block":
            return
        if "allow" in rules.values() and rules.get(application_id) != "allow":
            return
        now = self.clock()
        if now - self._last_seen.get(application_id, float("-inf")) < 5:
            return
        metadata: dict[str, float | bool] = {
            "is_fullscreen": window.is_fullscreen,
            "is_meeting_likely": window.is_meeting_likely,
        }
        if window.idle_seconds is not None:
            metadata["idle_seconds"] = window.idle_seconds
        signal = RawActivitySignal(
            source="window",
            application_id=application_id,
            window_title_summary=window.window_title_hint,
            activity_hint=window.ide_activity_hint,
            metadata=metadata,
        )
        if self._persist_and_publish(signal):
            self._last_seen[application_id] = now
            self._last_foreground_category = application_id

    def _persist_and_publish(self, signal: RawActivitySignal) -> bool:
        decision = self.privacy_policy.admit_activity(signal)
        if not decision.allowed or decision.event is None:
            return False
        if self.privacy_store.append_minimized_activity(decision.event):
            if self.health_monitor is not None:
                self.health_monitor.record_activity(decision.event)
            published = self.event_bus.publish(decision.event.envelope())
            if not published and self.health_monitor is not None:
                self.health_monitor.record_error("event_bus_rejected")
            return published
        return False


def _normalize_application_id(value: str) -> str:
    name = re.split(r"[\\/]", value.strip())[-1].casefold()
    return name.removesuffix(".exe") or "unknown"
