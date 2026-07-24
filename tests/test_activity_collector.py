from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nailong_agent.activity_collector import ForegroundWindow, IdleState, WindowActivityCollector
from nailong_agent.event_bus import EventBus
from nailong_agent.events import EventEnvelope, PetApplicationRule, PetPreferences
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore
from nailong_agent import windows_activity
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


class FakeIdleSource:
    def __init__(self) -> None:
        self.callback = None
        self.started = False

    def start(self, on_idle) -> None:
        self.callback = on_idle
        self.started = True

    def stop(self) -> None:
        self.started = False

    def emit(self, state: IdleState) -> None:
        assert self.callback is not None
        self.callback(state)


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
    source.emit(ForegroundWindow(process_id=1, executable_name="Code.exe", idle_seconds=12.5, is_fullscreen=True))

    assert bus.wait_idle(1.0)
    assert store.activity_count() == 1
    assert received[0].payload["application_id"] == "code"
    assert received[0].payload["activity"] == "unknown"
    assert received[0].payload["summary"] == "application=code; activity=unknown; source=window"
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


def test_collector_throttles_repeated_foreground_application(tmp_path: Path) -> None:
    source = FakeForegroundSource()
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    bus = EventBus()
    received: list[EventEnvelope] = []
    bus.subscribe("ActivityEvent", received.append)
    bus.start()
    clock_values = iter([0, 1])
    collector = WindowActivityCollector(
        source=source,
        privacy_policy=PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True)),
        privacy_store=store,
        event_bus=bus,
        preferences=lambda: PetPreferences(),
        application_rules=lambda: [],
        clock=lambda: next(clock_values),
    )

    collector.start()
    source.emit(ForegroundWindow(process_id=1, executable_name="Code.exe"))
    source.emit(ForegroundWindow(process_id=1, executable_name="Code.exe"))

    assert bus.wait_idle(1.0)
    assert store.activity_count() == 1
    assert len(received) == 1
    collector.stop()
    bus.stop()


def test_collector_persists_idle_state_after_threshold(tmp_path: Path) -> None:
    foreground_source = FakeForegroundSource()
    idle_source = FakeIdleSource()
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    bus = EventBus()
    received: list[EventEnvelope] = []
    bus.subscribe("ActivityEvent", received.append)
    bus.start()
    collector = WindowActivityCollector(
        source=foreground_source,
        idle_source=idle_source,
        privacy_policy=PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True)),
        privacy_store=store,
        event_bus=bus,
        preferences=lambda: PetPreferences(),
        application_rules=lambda: [],
    )

    collector.start()
    idle_source.emit(IdleState(idle_seconds=300))

    assert bus.wait_idle(1.0)
    assert idle_source.started is True
    assert store.activity_count() == 1
    assert received[0].payload["source"] == "idle"
    assert received[0].payload["application_id"] == "other"
    assert received[0].payload["activity"] == "idle"
    assert received[0].payload["summary"] == "application=other; activity=idle; source=idle"
    collector.stop()
    bus.stop()


def test_non_windows_foreground_source_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr("nailong_agent.windows_activity.os.name", "posix")
    source = create_foreground_source()

    source.start(lambda _: (_ for _ in ()).throw(AssertionError("unexpected callback")))
    source.stop()


def test_idle_seconds_handles_tick_counter_wraparound() -> None:
    assert windows_activity.idle_seconds_from_ticks(current_tick=500, last_input_tick=0xFFFF_FF00) == 0.756


def test_fullscreen_rectangle_requires_exact_monitor_coverage() -> None:
    assert windows_activity.is_fullscreen_rectangle((0, 0, 1920, 1080), (0, 0, 1920, 1080))
    assert not windows_activity.is_fullscreen_rectangle((0, 32, 1920, 1080), (0, 0, 1920, 1080))


def test_read_idle_seconds_uses_last_input_info() -> None:
    class User32:
        @staticmethod
        def GetLastInputInfo(info_pointer) -> int:
            info_pointer._obj.dwTime = 0xFFFF_FF00
            return 1

    class Kernel32:
        @staticmethod
        def GetTickCount() -> int:
            return 500

    assert windows_activity.read_idle_seconds(User32(), Kernel32()) == 0.756


def test_read_fullscreen_state_compares_window_and_monitor_rectangles() -> None:
    class User32:
        @staticmethod
        def GetWindowRect(_, rectangle_pointer) -> int:
            rectangle = rectangle_pointer._obj
            rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = (0, 0, 1920, 1080)
            return 1

        @staticmethod
        def MonitorFromWindow(_, __) -> int:
            return 1

        @staticmethod
        def GetMonitorInfoW(_, monitor_pointer) -> int:
            monitor = monitor_pointer._obj
            monitor.rcMonitor.left, monitor.rcMonitor.top = (0, 0)
            monitor.rcMonitor.right, monitor.rcMonitor.bottom = (1920, 1080)
            return 1

    assert windows_activity.read_fullscreen_state(User32(), 100)


def test_windows_idle_source_emits_once_per_continuous_idle_period() -> None:
    observed: list[IdleState] = []
    samples = iter([300, 420, 0, 300])
    source = windows_activity.WindowsIdleStateSource(
        idle_reader=lambda: next(samples),
        threshold_seconds=300,
    )
    source._callback = observed.append

    source._sample_once()
    source._sample_once()
    source._sample_once()
    source._sample_once()

    assert observed == [IdleState(idle_seconds=300), IdleState(idle_seconds=300)]


def test_windows_sources_hide_callback_errors_and_stop() -> None:
    errors: list[str] = []
    foreground_source = windows_activity.WindowsForegroundActivitySource(
        on_error=lambda error: errors.append(str(error))
    )
    idle_source = windows_activity.WindowsIdleStateSource(
        idle_reader=lambda: 300,
        on_error=lambda error: errors.append(str(error)),
    )

    foreground_source._handle_callback_failure(RuntimeError("private process path"))
    idle_source._handle_callback_failure(RuntimeError("private idle details"))

    assert foreground_source.stopped is True
    assert idle_source.stopped is True
    assert errors == ["activity source failed", "activity source failed"]
