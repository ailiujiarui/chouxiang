from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from nailong_agent.event_bus import EventBus
from nailong_agent.events import PetApplicationRule, PetPreferences, RawActivitySignal
from nailong_agent.privacy import PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore


@dataclass(frozen=True)
class ForegroundWindow:
    process_id: int
    executable_name: str
    idle_seconds: float | None = None
    is_fullscreen: bool = False
    is_meeting_likely: bool = False


class ForegroundActivitySource(Protocol):
    def start(self, on_change: Callable[[ForegroundWindow], None]) -> None: ...

    def stop(self) -> None: ...


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
        self._started = False
        self._last_seen: dict[str, float] = {}

    def start(self) -> None:
        if self._started:
            return
        self.source.start(self._on_foreground_change)
        if self.idle_source is not None:
            self.idle_source.start(self._on_idle_change)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self.idle_source is not None:
            self.idle_source.stop()
        self.source.stop()

    def _on_foreground_change(self, window: ForegroundWindow) -> None:
        try:
            self._collect(window)
        except Exception as exc:
            self.stop()
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
            metadata=metadata,
        )
        if self._persist_and_publish(signal):
            self._last_seen[application_id] = now

    def _persist_and_publish(self, signal: RawActivitySignal) -> bool:
        decision = self.privacy_policy.admit_activity(signal)
        if not decision.allowed or decision.event is None:
            return False
        if self.privacy_store.append_minimized_activity(decision.event):
            self.event_bus.publish(decision.event.envelope())
            return True
        return False


def _normalize_application_id(value: str) -> str:
    name = re.split(r"[\\/]", value.strip())[-1].casefold()
    return name.removesuffix(".exe") or "unknown"
