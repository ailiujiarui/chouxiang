from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep

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


def test_due_window_is_processed_without_a_followup_activity_event(tmp_path: Path) -> None:
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
    orchestrator.on_activity_event(_coding_event("event-1", 1).envelope())

    assert orchestrator.flush_due(datetime(2026, 7, 24, 8, 0, 59, tzinfo=timezone.utc)) is False
    assert notifications.get_status().pending_count == 0
    assert orchestrator.flush_due(datetime(2026, 7, 24, 8, 1, tzinfo=timezone.utc)) is True
    assert notifications.get_status().pending_count == 1
    assert orchestrator.flush_due(datetime(2026, 7, 24, 8, 2, tzinfo=timezone.utc)) is False
    assert notifications.get_status().pending_count == 1


def test_scheduler_start_stop_is_idempotent_and_processes_due_window(tmp_path: Path) -> None:
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
        scheduler_interval_seconds=0.01,
        clock=lambda: datetime(2026, 7, 24, 8, 1, tzinfo=timezone.utc),
    )
    orchestrator.on_activity_event(_coding_event("event-1", 1).envelope())

    orchestrator.start()
    first_thread = orchestrator._thread
    orchestrator.start()
    assert orchestrator._thread is first_thread
    deadline = monotonic() + 1
    while notifications.get_status().pending_count == 0 and monotonic() < deadline:
        sleep(0.01)
    orchestrator.stop()
    orchestrator.stop()

    assert notifications.get_status().pending_count == 1
    assert first_thread is not None and not first_thread.is_alive()


def test_game_tease_is_queued_only_after_preference_and_five_minutes(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "notifications.sqlite")
    store.save_preferences(store.get_preferences().model_copy(update={"game_tease_enabled": True}))
    notifications = NotificationService(
        store=store,
        policy=NotificationPolicy(minimum_cooldown_seconds=0, maximum_cooldown_seconds=0),
        clock=lambda: datetime(2026, 7, 24, 8, 7, tzinfo=timezone.utc),
        minimum_popup_start_spacing_seconds=0,
    )
    orchestrator = ActivityPersonalityOrchestrator(
        aggregator_window_seconds=60,
        personality_agent=PetPersonalityAgent(),
        notifications=notifications,
    )

    orchestrator.on_activity_event(_gaming_event("game-1", 0).envelope())
    orchestrator.on_activity_event(_gaming_event("game-2", 301).envelope())
    assert notifications.get_status().pending_count == 0

    orchestrator.on_activity_event(_gaming_event("game-3", 361).envelope())
    assert notifications.get_status().pending_count == 1

    orchestrator.on_activity_event(_gaming_event("game-4", 421).envelope())
    assert notifications.get_status().pending_count == 1


def test_transient_shell_focus_does_not_reset_game_session(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "notifications.sqlite")
    store.save_preferences(store.get_preferences().model_copy(update={"game_tease_enabled": True}))
    notifications = NotificationService(
        store=store,
        policy=NotificationPolicy(minimum_cooldown_seconds=0, maximum_cooldown_seconds=0),
        clock=lambda: datetime(2026, 7, 24, 8, 7, tzinfo=timezone.utc),
        minimum_popup_start_spacing_seconds=0,
    )
    orchestrator = ActivityPersonalityOrchestrator(
        aggregator_window_seconds=60,
        personality_agent=PetPersonalityAgent(),
        notifications=notifications,
    )

    orchestrator.on_activity_event(_gaming_event("game-1", 0).envelope())
    orchestrator.on_activity_event(_application_event("other-1", 10, "other").envelope())
    orchestrator.on_activity_event(_application_event("explorer-1", 20, "explorer").envelope())
    orchestrator.on_activity_event(_gaming_event("game-2", 301).envelope())
    orchestrator.on_activity_event(_gaming_event("game-3", 361).envelope())

    assert notifications.get_status().pending_count == 1


def test_sustained_explicit_non_game_activity_resets_game_session(tmp_path: Path) -> None:
    orchestrator = ActivityPersonalityOrchestrator(
        personality_agent=PetPersonalityAgent(),
        notifications=NotificationService(store=NotificationStore(tmp_path / "notifications.sqlite")),
    )

    orchestrator.on_activity_event(_gaming_event("game-1", 0).envelope())
    orchestrator.on_activity_event(_application_event("browser-1", 10, "browser").envelope())
    assert orchestrator._gaming_started_at is not None

    orchestrator.flush_due(datetime(2026, 7, 24, 8, 0, 24, tzinfo=timezone.utc))
    assert orchestrator._gaming_started_at is not None

    orchestrator.flush_due(datetime(2026, 7, 24, 8, 0, 25, tzinfo=timezone.utc))
    assert orchestrator._gaming_started_at is None


def test_game_tease_becomes_due_without_followup_foreground_event(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "notifications.sqlite")
    store.save_preferences(store.get_preferences().model_copy(update={"game_tease_enabled": True}))
    notifications = NotificationService(
        store=store,
        policy=NotificationPolicy(minimum_cooldown_seconds=0, maximum_cooldown_seconds=0),
        clock=lambda: datetime(2026, 7, 24, 8, 6, tzinfo=timezone.utc),
        minimum_popup_start_spacing_seconds=0,
    )
    orchestrator = ActivityPersonalityOrchestrator(
        personality_agent=PetPersonalityAgent(),
        notifications=notifications,
    )
    orchestrator.on_activity_event(_gaming_event("game-1", 0).envelope())

    assert orchestrator.process_due_game_session(
        datetime(2026, 7, 24, 8, 4, 59, tzinfo=timezone.utc)
    ) is False
    assert notifications.get_status().pending_count == 0

    assert orchestrator.process_due_game_session(
        datetime(2026, 7, 24, 8, 5, tzinfo=timezone.utc)
    ) is True
    assert notifications.get_status().pending_count == 1


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


def _gaming_event(event_id: str, seconds: int) -> ActivityEvent:
    return ActivityEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        source="window",
        application_id="game",
        activity=ActivityType.GAMING,
        confidence=0.95,
        summary="application=game; activity=gaming; source=window",
    )


def _application_event(event_id: str, seconds: int, application_id: str) -> ActivityEvent:
    return ActivityEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        source="window",
        application_id=application_id,
        activity=ActivityType.UNKNOWN,
        confidence=0.0,
        summary=f"application={application_id}; activity=unknown; source=window",
    )
