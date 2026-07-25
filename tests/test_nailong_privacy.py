from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from pydantic import ValidationError

from nailong_agent.events import (
    ActivityEvent,
    ActivityType,
    ActivityWindow,
    RawActivitySignal,
)
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore


def test_collection_is_denied_until_user_explicitly_consents() -> None:
    policy = PrivacyPolicy()

    decision = policy.admit_activity(RawActivitySignal(source="window", application_id="code"))

    assert policy.needs_initial_consent is True
    assert decision.allowed is False
    assert decision.reason == "activity_collection_not_authorized"


def test_unanswered_consent_is_distinct_from_a_recorded_decline() -> None:
    assert PrivacyPolicy().needs_initial_consent is True
    assert PrivacyPolicy(PrivacyConsent()).needs_initial_consent is False


def test_sensitive_and_meeting_windows_are_blocked_before_unified_event_creation() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True, remote_inference_enabled=True))

    password = policy.admit_activity(
        RawActivitySignal(source="window", application_id="code", window_title_summary=".env - API_TOKEN=secret")
    )
    ssh = policy.admit_activity(
        RawActivitySignal(source="window", application_id="explorer", window_title_summary="C:\\Users\\me\\.ssh\\id_rsa")
    )
    meeting = policy.admit_activity(RawActivitySignal(source="window", application_id="teams"))

    assert (password.allowed, password.reason) == (False, "sensitive_window_or_content")
    assert (ssh.allowed, ssh.reason) == (False, "sensitive_window_or_content")
    assert (meeting.allowed, meeting.reason) == (False, "meeting_window")


def test_false_meeting_signal_does_not_block_collection() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True))

    decision = policy.admit_activity(
        RawActivitySignal(source="window", application_id="code", metadata={"is_meeting_likely": False})
    )

    assert decision.allowed is True


def test_allowed_signal_becomes_unified_minimized_activity() -> None:
    signal = RawActivitySignal(
        source="window",
        application_id="C:\\Program Files\\Microsoft VS Code\\Code.exe",
        window_title_summary="customer-project - main.py",
        activity_hint="editing billing code",
        metadata={"idle_seconds": 12, "raw_command": "python private.py"},
    )
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True))

    decision = policy.admit_activity(signal)

    assert decision.allowed is True
    assert decision.event is not None
    assert decision.event.application_id == "code"
    assert decision.event.activity == ActivityType.CODING
    assert decision.event.confidence == 0.7
    assert decision.event.summary == "application=code; activity=coding; source=window"
    assert "private.py" not in decision.event.model_dump_json()
    assert policy.prepare_remote_summary(decision.event) is None


def test_unknown_application_is_reduced_to_a_nonidentifying_category() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True))

    decision = policy.admit_activity(
        RawActivitySignal(source="process", application_id="C:\\Customers\\Acme Internal Tool.exe")
    )

    assert decision.allowed is True
    assert decision.event is not None
    assert decision.event.application_id == "other"
    assert "acme" not in decision.event.model_dump_json().casefold()


def test_remote_summary_accepts_only_unified_event_and_is_redacted() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True, remote_inference_enabled=True))
    decision = policy.admit_activity(RawActivitySignal(source="idle", application_id="Code.exe"))

    assert decision.event is not None
    assert policy.prepare_remote_summary(decision.event) == "application=code; activity=idle; source=idle"
    assert "[REDACTED]" in policy.redact_text_for_remote("token=abc123 C:\\secret\\file")
    assert "C:\\secret" not in policy.redact_text_for_remote("token=abc123 C:\\secret\\file")
    assert "secret-token" not in policy.redact_text_for_remote("Authorization: Bearer secret-token-value")


@pytest.mark.parametrize("forbidden", ["raw_code", "clipboard", "screenshot", "window_title_summary", "metadata"])
def test_unified_event_rejects_raw_content_fields(forbidden: str) -> None:
    payload = {
        "source": "window",
        "application_id": "code",
        "activity": "coding",
        "confidence": 0.8,
        forbidden: "secret",
    }
    with pytest.raises(ValidationError):
        ActivityEvent.model_validate(payload)


def test_unified_event_rejects_naive_time_and_unsafe_application() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ActivityEvent(
            occurred_at=datetime(2026, 7, 24, 8, 0),
            source="window",
            application_id="code",
            activity=ActivityType.CODING,
            confidence=0.8,
        )
    with pytest.raises(ValidationError):
        ActivityEvent(
            source="window",
            application_id="C:\\private\\code.exe",
            activity=ActivityType.CODING,
            confidence=0.8,
        )


def test_store_persists_unified_events_and_windows_then_clears_both(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    consent = PrivacyConsent(activity_collection_enabled=True)
    policy = PrivacyPolicy(consent)
    store.save_consent(consent)
    decision = policy.admit_activity(RawActivitySignal(source="window", application_id="code", activity=ActivityType.CODING, confidence=0.8))
    assert decision.event is not None
    store.append_minimized_activity(decision.event)
    started = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    store.append_activity_window(
        ActivityWindow(
            window_started_at=started,
            window_ended_at=started + timedelta(seconds=60),
            dominant_application="code",
            dominant_activity=ActivityType.CODING,
            confidence=0.8,
            event_count=1,
            summary=decision.event.summary,
        )
    )

    assert store.load_consent() == consent
    assert store.activity_count() == 1
    assert store.activity_window_count() == 1
    assert store.list_activity_windows()[0].dominant_activity == ActivityType.CODING
    assert store.clear_activity_history() == 2
    assert store.activity_count() == 0
    assert store.activity_window_count() == 0


def test_store_rejects_events_that_bypass_minimization(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    private = ActivityEvent(
        source="window",
        application_id="code",
        activity=ActivityType.CODING,
        confidence=0.8,
        sensitivity="private",
    )
    with pytest.raises(ValueError, match="minimized"):
        store.append_minimized_activity(private)

    bypassed = ActivityEvent.model_construct(
        event_id="unsafe",
        occurred_at=datetime.now(timezone.utc),
        source="window",
        application_id="C:\\private\\code.exe",
        activity=ActivityType.CODING,
        confidence=0.8,
        summary=None,
        sensitivity="public",
    )
    with pytest.raises(ValueError, match="minimized"):
        store.append_minimized_activity(bypassed)


def test_store_is_idempotent_by_event_id(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    event = ActivityEvent(
        event_id="activity-1",
        source="window",
        application_id="code",
        activity=ActivityType.CODING,
        confidence=0.9,
    )

    assert store.append_minimized_activity(event) is True
    assert store.append_minimized_activity(event) is False


def test_store_migrates_legacy_activity_rows_without_losing_data(tmp_path) -> None:
    database_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE pet_privacy_consent (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                activity_collection_enabled INTEGER NOT NULL,
                remote_inference_enabled INTEGER NOT NULL
            );
            INSERT INTO pet_privacy_consent VALUES (1, 1, 0);
            CREATE TABLE pet_activity_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                source TEXT NOT NULL,
                application_id TEXT NOT NULL,
                idle_seconds INTEGER,
                is_fullscreen INTEGER NOT NULL CHECK (is_fullscreen IN (0, 1)),
                is_meeting_likely INTEGER NOT NULL CHECK (is_meeting_likely IN (0, 1))
            );
            INSERT INTO pet_activity_events
                (event_id, occurred_at, source, application_id, idle_seconds,
                 is_fullscreen, is_meeting_likely)
            VALUES ('legacy-event', '2026-07-24T08:00:00+00:00', 'window', 'code', 3, 0, 0);
            """
        )

    store = PrivacyStore(database_path)

    assert store.load_consent() == PrivacyConsent(activity_collection_enabled=True)
    assert store.activity_count() == 1
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(pet_activity_events)")}
        row = connection.execute(
            "SELECT event_id, activity, confidence, summary FROM pet_activity_events"
        ).fetchone()
    assert {"activity", "confidence", "summary"} <= columns
    assert row == ("legacy-event", "unknown", 0.0, None)


def test_store_migrates_intermediate_activity_windows(tmp_path) -> None:
    database_path = tmp_path / "intermediate.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE pet_activity_windows (
                application_id TEXT NOT NULL,
                activity TEXT NOT NULL,
                window_started_at TEXT NOT NULL,
                last_occurred_at TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                maximum_confidence REAL NOT NULL,
                PRIMARY KEY (application_id, activity, window_started_at)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pet_activity_windows
            VALUES ('code', 'coding', '2026-07-24T08:00:00+00:00',
                    '2026-07-24T08:01:00+00:00', 2, 0.9)
            """
        )

    store = PrivacyStore(database_path)

    windows = store.list_activity_windows()
    assert len(windows) == 1
    assert windows[0].dominant_application == "code"
    assert windows[0].dominant_activity == ActivityType.CODING
    assert windows[0].event_count == 2
    assert windows[0].confidence == 0.9
