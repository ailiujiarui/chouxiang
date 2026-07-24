from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from nailong_agent.events import ActivityClassification, ActivityEvent
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore


def test_collection_is_denied_until_user_explicitly_consents() -> None:
    policy = PrivacyPolicy()

    decision = policy.admit_activity(ActivityEvent(source="window", application_id="code"))

    assert policy.needs_initial_consent is True
    assert decision.allowed is False
    assert decision.reason == "activity_collection_not_authorized"


def test_unanswered_consent_is_distinct_from_a_recorded_decline() -> None:
    assert PrivacyPolicy().needs_initial_consent is True
    assert PrivacyPolicy(PrivacyConsent()).needs_initial_consent is False


def test_sensitive_and_meeting_windows_are_blocked_before_storage_or_model() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True, remote_inference_enabled=True))

    password = policy.admit_activity(
        ActivityEvent(source="window", application_id="code", window_title_summary=".env — API_TOKEN=secret")
    )
    ssh = policy.admit_activity(ActivityEvent(source="window", application_id="explorer", window_title_summary="C:\\Users\\me\\.ssh\\id_rsa"))
    meeting = policy.admit_activity(ActivityEvent(source="window", application_id="teams"))

    assert (password.allowed, password.reason) == (False, "sensitive_window_or_content")
    assert (ssh.allowed, ssh.reason) == (False, "sensitive_window_or_content")
    assert (meeting.allowed, meeting.reason) == (False, "meeting_window")
    assert policy.prepare_remote_summary(password.event or ActivityEvent(source="window", application_id="teams")) is None


def test_allowed_activity_is_minimized_and_remote_summary_is_local_by_default() -> None:
    event = ActivityEvent(
        source="window",
        application_id="C:\\Program Files\\Microsoft VS Code\\Code.exe",
        window_title_summary="customer-project — main.py",
        activity_hint="editing billing code",
        metadata={"idle_seconds": 12, "raw_command": "python private.py"},
    )
    local_only = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True))

    decision = local_only.admit_activity(event)

    assert decision.allowed is True
    assert decision.event is not None
    assert decision.event.application_id == "code"
    assert decision.event.window_title_summary is None
    assert decision.event.activity_hint is None
    assert decision.event.metadata == {"idle_seconds": 12}
    assert local_only.prepare_remote_summary(event) is None


def test_false_meeting_signal_does_not_block_collection() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True))

    decision = policy.admit_activity(
        ActivityEvent(source="window", application_id="code", metadata={"is_meeting_likely": False})
    )

    assert decision.allowed is True


def test_remote_summary_is_minimized_and_text_redaction_is_defence_in_depth() -> None:
    policy = PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True, remote_inference_enabled=True))
    event = ActivityEvent(source="idle", application_id="Code.exe", metadata={"idle_seconds": 60})

    summary = policy.prepare_remote_summary(event)

    assert summary == "application=code; source=idle; signals=idle_seconds"
    assert "[REDACTED]" in policy.redact_text_for_remote("token=abc123 C:\\secret\\file")
    assert "C:\\secret" not in policy.redact_text_for_remote("token=abc123 C:\\secret\\file")
    assert "secret-token" not in policy.redact_text_for_remote("Authorization: Bearer secret-token-value")


def test_store_persists_only_minimized_events_and_deletes_all_activity_history(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    consent = PrivacyConsent(activity_collection_enabled=True)
    policy = PrivacyPolicy(consent)
    store.save_consent(consent)
    decision = policy.admit_activity(
        ActivityEvent(source="window", application_id="code", window_title_summary="private.py", metadata={"idle_seconds": 1})
    )

    assert store.load_consent() == consent
    assert decision.event is not None
    store.append_minimized_activity(decision.event)
    assert store.activity_count() == 1
    assert store.clear_activity_history() == 1
    assert store.activity_count() == 0


def test_store_rejects_events_that_bypass_minimization(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    raw = ActivityEvent(source="window", application_id="code", metadata={"raw_command": "secret"})

    with pytest.raises(ValueError, match="minimized"):
        store.append_minimized_activity(raw)

    with pytest.raises(ValueError, match="minimized"):
        store.append_minimized_activity(ActivityEvent(source="window", application_id="C:\\private\\code.exe"))


def test_store_deduplicates_and_aggregates_minimized_activity(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    occurred_at = datetime(2026, 7, 24, 9, 1, tzinfo=timezone.utc)
    classification = ActivityClassification(activity="coding", confidence=0.9)
    first = ActivityEvent(
        event_id="activity-1",
        occurred_at=occurred_at,
        source="window",
        application_id="code",
        metadata={"idle_seconds": 0},
    )
    duplicate = first.model_copy(update={"event_id": "activity-2"})

    assert store.append_minimized_activity(first, classification=classification) is True
    assert store.append_minimized_activity(duplicate, classification=classification) is False

    windows = store.list_activity_windows()
    assert len(windows) == 1
    assert windows[0].application_id == "code"
    assert windows[0].activity == "coding"
    assert windows[0].event_count == 1
    assert windows[0].maximum_confidence == 0.9


def test_clear_activity_history_removes_aggregates_but_preserves_consent(tmp_path) -> None:
    store = PrivacyStore(tmp_path / "pet.sqlite")
    consent = PrivacyConsent(activity_collection_enabled=True)
    store.save_consent(consent)
    store.append_minimized_activity(
        ActivityEvent(source="window", application_id="code"),
        classification=ActivityClassification(activity="coding", confidence=0.9),
    )

    assert store.clear_activity_history() == 1
    assert store.list_activity_windows() == []
    assert store.load_consent() == consent


def test_store_migrates_existing_privacy_database_without_losing_consent(tmp_path) -> None:
    database = tmp_path / "pet.sqlite"
    with sqlite3.connect(database) as connection:
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
                is_fullscreen INTEGER NOT NULL,
                is_meeting_likely INTEGER NOT NULL
            );
            """
        )

    store = PrivacyStore(database)

    assert store.load_consent() == PrivacyConsent(activity_collection_enabled=True)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(pet_activity_events)")}
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"activity", "confidence", "fingerprint"}.issubset(columns)
    assert "pet_activity_windows" in tables
