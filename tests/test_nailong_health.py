from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nailong_agent.events import ActivityEvent, ActivityType, NotificationStatus, PetPreferences
from nailong_agent.health import NailongHealthMonitor
from nailong_agent.privacy import PrivacyConsent


NOW = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)


def _status(**updates: object) -> NotificationStatus:
    return NotificationStatus(
        do_not_disturb=False,
        last_consumed_sequence=0,
        pending_count=0,
        displaying_count=0,
        suppressed_terminal_count=0,
        remaining_daily_popup_budget=12,
    ).model_copy(update=updates)


def test_health_snapshot_explains_game_and_fullscreen_silence_without_raw_content() -> None:
    monitor = NailongHealthMonitor()
    monitor.record_listener(running=True)
    monitor.record_activity(
        ActivityEvent(
            event_id="game-event",
            occurred_at=NOW,
            source="window",
            application_id="game",
            activity=ActivityType.GAMING,
            confidence=0.95,
            summary="application=game; activity=gaming; source=window",
        )
    )

    disabled = monitor.snapshot(
        preferences=PetPreferences(game_tease_enabled=False),
        status=_status(),
        consent=PrivacyConsent(activity_collection_enabled=True),
        fullscreen_blocked=True,
        now=NOW,
    )
    enabled = monitor.snapshot(
        preferences=PetPreferences(game_tease_enabled=True),
        status=_status(),
        consent=PrivacyConsent(activity_collection_enabled=True),
        fullscreen_blocked=True,
        now=NOW,
    )
    pending = monitor.snapshot(
        preferences=PetPreferences(game_tease_enabled=True),
        status=_status(pending_count=1),
        consent=PrivacyConsent(activity_collection_enabled=True),
        fullscreen_blocked=True,
        now=NOW,
    )

    assert disabled.silence_reason == "game_tease_disabled"
    assert enabled.silence_reason == "game_tease_waiting"
    assert pending.silence_reason == "pending_delivery"
    summary = pending.redacted_summary()
    assert "game" in summary
    assert "League of Legends" not in summary
    assert "game-event" not in summary


def test_health_snapshot_distinguishes_policy_and_runtime_reasons() -> None:
    monitor = NailongHealthMonitor()
    consent = PrivacyConsent(activity_collection_enabled=True)
    preferences = PetPreferences()

    assert monitor.snapshot(preferences=preferences, status=_status(), consent=consent).silence_reason == "listener_stopped"
    monitor.record_listener(running=True)
    assert monitor.snapshot(
        preferences=preferences,
        status=_status(),
        consent=PrivacyConsent(),
    ).silence_reason == "privacy_not_authorized"
    assert monitor.snapshot(
        preferences=preferences.model_copy(update={"manual_pause_enabled": True}),
        status=_status(),
        consent=consent,
    ).silence_reason == "manual_pause"
    assert monitor.snapshot(
        preferences=preferences,
        status=_status(do_not_disturb=True),
        consent=consent,
    ).silence_reason == "do_not_disturb"
    assert monitor.snapshot(
        preferences=preferences,
        status=_status(next_regular_at=NOW + timedelta(minutes=1)),
        consent=consent,
        now=NOW,
    ).silence_reason == "cooldown"


def test_listener_error_is_reported_without_exception_details() -> None:
    monitor = NailongHealthMonitor()
    monitor.record_listener(running=False, error_code="listener_error")

    snapshot = monitor.snapshot(
        preferences=PetPreferences(),
        status=_status(),
        consent=PrivacyConsent(activity_collection_enabled=True),
    )

    assert snapshot.silence_reason == "listener_error"
    assert snapshot.last_error_code == "listener_error"


def test_health_monitor_reduces_untrusted_diagnostic_values_to_fixed_enums() -> None:
    monitor = NailongHealthMonitor()
    monitor.record_classification(
        application_category="C:/private/customer.exe",
        activity_type="secret project content",
        confidence=0.5,
    )
    monitor.record_error("token=private-value")

    snapshot = monitor.snapshot()

    assert snapshot.last_application_category == "other"
    assert snapshot.last_activity_type == "unknown"
    assert snapshot.last_error_code == "runtime_error"
    assert "customer" not in snapshot.redacted_summary()
    assert "private-value" not in snapshot.redacted_summary()


def test_health_snapshot_exposes_python_review_state_without_untrusted_values() -> None:
    monitor = NailongHealthMonitor()
    monitor.record_python_review(
        enabled=True,
        status="failed",
        error_code="token=private-value",
        silence_reason="private-project-content",
    )

    snapshot = monitor.snapshot(consent=PrivacyConsent(python_review_enabled=True))

    assert snapshot.python_review_enabled is True
    assert snapshot.python_review_last_status == "failed"
    assert snapshot.python_review_last_error_code == "runtime_error"
    assert snapshot.python_review_silence_reason == "not_authorized"
    assert "private-value" not in snapshot.redacted_summary()
