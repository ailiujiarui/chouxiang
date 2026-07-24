from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nailong_agent.activity_personality_orchestrator import ActivityPersonalityOrchestrator
from nailong_agent.app import DesktopProcess
from nailong_agent.event_bus import EventBus
from nailong_agent.events import ActivityEvent, ActivityType, PetExpression
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.personality_agent import PetPersonalityAgent
from nailong_agent.pet_state import PetEmotion
from nailong_agent.renderer import NullRenderer


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def test_personality_state_survives_reopen_and_expires_to_neutral(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc))
    database = tmp_path / "notifications.sqlite"
    service = NotificationService(store=NotificationStore(database), clock=clock)

    saved = service.update_personality_state(
        emotion=PetEmotion.CONCERNED,
        task_id="activity-window:123",
        expires_in_seconds=300,
    )

    assert saved.emotion is PetEmotion.CONCERNED
    assert saved.task_id == "activity-window:123"
    assert NotificationService(store=NotificationStore(database), clock=clock).get_personality_state() == saved

    clock.advance(301)
    expired = service.get_personality_state()

    assert expired.emotion is PetEmotion.NEUTRAL
    assert expired.task_id is None
    assert expired.expires_at is None


def test_orchestrator_persists_emotion_when_personality_chooses_silence(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 24, 8, 2, tzinfo=timezone.utc))
    notifications = NotificationService(
        store=NotificationStore(tmp_path / "notifications.sqlite"),
        clock=clock,
    )
    bus = EventBus()
    orchestrator = ActivityPersonalityOrchestrator(
        personality_agent=PetPersonalityAgent(),
        notifications=notifications,
    )
    bus.subscribe("ActivityEvent", orchestrator.on_activity_event)
    bus.start()
    try:
        assert bus.publish(_coding_event("event-1", 0).envelope())
        assert bus.publish(_coding_event("event-2", 61).envelope())
        assert bus.wait_idle(1.0)
    finally:
        bus.stop()

    state = notifications.get_personality_state()
    assert state.emotion is PetEmotion.CURIOUS
    assert state.task_id is not None
    assert notifications.get_status().pending_count == 0


def test_desktop_process_restores_unexpired_personality_state(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 24, 8, 2, tzinfo=timezone.utc))
    notifications = NotificationService(
        store=NotificationStore(tmp_path / "notifications.sqlite"),
        clock=clock,
    )
    notifications.update_personality_state(
        emotion=PetEmotion.CONCERNED,
        task_id="activity-window:123",
        expires_in_seconds=300,
    )
    renderer = NullRenderer()
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=lambda: renderer,
        notification_service=notifications,
    )

    assert process.run() == 0
    assert renderer.states[-1].expression is PetExpression.CONCERNED
    assert renderer.states[-1].bubble_visible is False


def _coding_event(event_id: str, seconds: int) -> ActivityEvent:
    return ActivityEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        source="window",
        application_id="code",
        activity=ActivityType.CODING,
        confidence=0.95,
        summary="application=code; activity=coding; source=window",
    )
