from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nailong_agent.activity_personality_orchestrator import ActivityPersonalityOrchestrator
from nailong_agent.delivery import NotificationDeliveryPump
from nailong_agent.event_bus import EventBus
from nailong_agent.events import ActivityEvent, ActivityType, PopupDecision
from nailong_agent.notification_policy import NotificationPolicy
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.personality_agent import PetPersonalityAgent


def test_activity_windows_flow_through_personality_and_delivery(tmp_path: Path) -> None:
    bus = EventBus()
    notifications = NotificationService(
        store=NotificationStore(tmp_path / "notifications.sqlite"),
        policy=NotificationPolicy(minimum_cooldown_seconds=0, maximum_cooldown_seconds=0),
        clock=lambda: datetime(2026, 7, 24, 8, 2, tzinfo=timezone.utc),
        minimum_popup_start_spacing_seconds=0,
    )
    orchestrator = ActivityPersonalityOrchestrator(
        aggregator_window_seconds=60,
        personality_agent=PetPersonalityAgent(),
        notifications=notifications,
    )
    decisions: list[PopupDecision] = []
    bus.subscribe("ActivityEvent", orchestrator.on_activity_event)
    bus.subscribe("PopupDecision", lambda envelope: decisions.append(PopupDecision.model_validate(envelope.payload)))
    bus.start()
    try:
        assert bus.publish(_debugging_event("event-1", 0).envelope())
        assert bus.publish(_debugging_event("event-2", 61).envelope())
        assert bus.wait_idle(1.0)

        assert notifications.get_status().pending_count == 1
        assert NotificationDeliveryPump(notifications=notifications, bus=bus).run_once() is True
        assert bus.wait_idle(1.0)
    finally:
        bus.stop()

    assert len(decisions) == 1
    assert decisions[0].action == "show"
    assert decisions[0].reason == "proactive:encouragement"
    assert decisions[0].message


def _debugging_event(event_id: str, seconds: int) -> ActivityEvent:
    return ActivityEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        source="window",
        application_id="code",
        activity=ActivityType.DEBUGGING,
        confidence=0.95,
        summary="application=code; activity=debugging; source=window",
    )
