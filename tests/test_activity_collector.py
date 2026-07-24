from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nailong_agent.activity_collector import ForegroundWindow, WindowActivityCollector
from nailong_agent.event_bus import EventBus
from nailong_agent.events import EventEnvelope, PetApplicationRule, PetPreferences
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore
from nailong_agent.windows_activity import create_foreground_source


class FakeForegroundSource:
    def __init__(self) -> None:
        self.callback = None
        self.started = False

    def start(self, on_change) -> None:
        self.callback = on_change
        self.started = True

    def stop(self) -> None:
        self.started = False

    def emit(self, window: ForegroundWindow) -> None:
        assert self.callback is not None
        self.callback(window)


def test_collector_persists_and_publishes_only_minimized_event(tmp_path: Path) -> None:
    source = FakeForegroundSource()
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    bus = EventBus()
    received: list[EventEnvelope] = []
    bus.subscribe("ActivityEvent", received.append)
    bus.start()
    collector = WindowActivityCollector(
        source=source,
        privacy_policy=PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True)),
        privacy_store=store,
        event_bus=bus,
        preferences=lambda: PetPreferences(),
        application_rules=lambda: [],
    )

    collector.start()
    source.emit(ForegroundWindow(process_id=1, executable_name="Code.exe"))

    assert bus.wait_idle(1.0)
    assert store.activity_count() == 1
    assert received[0].payload["application_id"] == "code"
    assert received[0].payload["window_title_summary"] is None
    collector.stop()
    bus.stop()


def test_collector_blocks_paused_and_blacklisted_applications(tmp_path: Path) -> None:
    source = FakeForegroundSource()
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    bus = EventBus()
    bus.start()
    collector = WindowActivityCollector(
        source=source,
        privacy_policy=PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True)),
        privacy_store=store,
        event_bus=bus,
        preferences=lambda: PetPreferences(manual_pause_enabled=True),
        application_rules=lambda: [PetApplicationRule(application_id="code", rule="block")],
    )

    collector.start()
    source.emit(ForegroundWindow(process_id=1, executable_name="Code.exe"))

    assert store.activity_count() == 0
    collector.stop()
    bus.stop()


def test_non_windows_foreground_source_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr("nailong_agent.windows_activity.os.name", "posix")
    source = create_foreground_source()

    source.start(lambda _: (_ for _ in ()).throw(AssertionError("unexpected callback")))
    source.stop()
