"""Local-only persistence for Nailong privacy consent and minimized activity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nailong_agent.events import ActivityClassification, ActivityEvent
from nailong_agent.privacy import PrivacyConsent

_PERSISTABLE_METADATA_KEYS = frozenset({"idle_seconds", "is_fullscreen", "is_meeting_likely"})
_APPLICATION_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ACTIVITY_WINDOW_SECONDS = 5 * 60


@dataclass(frozen=True)
class ActivityWindow:
    application_id: str
    activity: str
    window_started_at: datetime
    last_occurred_at: datetime
    event_count: int
    maximum_confidence: float


class PrivacyStore:
    """A separate SQLite store that never receives raw desktop content."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def load_consent(self) -> PrivacyConsent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT activity_collection_enabled, remote_inference_enabled, decision_recorded
                FROM pet_privacy_consent WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return PrivacyConsent(bool(row[0]), bool(row[1]), bool(row[2]))

    def save_consent(self, consent: PrivacyConsent) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pet_privacy_consent
                    (id, activity_collection_enabled, remote_inference_enabled, decision_recorded)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    activity_collection_enabled = excluded.activity_collection_enabled,
                    remote_inference_enabled = excluded.remote_inference_enabled,
                    decision_recorded = excluded.decision_recorded
                """,
                (int(consent.activity_collection_enabled), int(consent.remote_inference_enabled), int(consent.decision_recorded)),
            )

    def append_minimized_activity(
        self,
        event: ActivityEvent,
        *,
        classification: ActivityClassification | None = None,
    ) -> bool:
        if (
            event.sensitivity != "public"
            or event.window_title_summary is not None
            or event.activity_hint is not None
            or not set(event.metadata).issubset(_PERSISTABLE_METADATA_KEYS)
            or not _APPLICATION_CATEGORY.fullmatch(event.application_id)
        ):
            raise ValueError("only minimized public activity events may be persisted")
        occurred_at = _as_utc(event.occurred_at)
        activity = classification.activity if classification is not None else "unknown"
        confidence = classification.confidence if classification is not None else 0.0
        window_started_at = _window_started_at(occurred_at)
        fingerprint = _activity_fingerprint(event, activity, window_started_at)
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO pet_activity_events
                    (
                        event_id, occurred_at, source, application_id, idle_seconds,
                        is_fullscreen, is_meeting_likely, activity, confidence, fingerprint
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    occurred_at.isoformat(),
                    event.source,
                    event.application_id,
                    _nonnegative_int(event.metadata.get("idle_seconds")),
                    int(bool(event.metadata.get("is_fullscreen"))),
                    int(bool(event.metadata.get("is_meeting_likely"))),
                    activity,
                    confidence,
                    fingerprint,
                ),
            ).rowcount
            if not inserted:
                return False
            connection.execute(
                """
                INSERT INTO pet_activity_windows (
                    application_id, activity, window_started_at, last_occurred_at,
                    event_count, maximum_confidence
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(application_id, activity, window_started_at) DO UPDATE SET
                    last_occurred_at = excluded.last_occurred_at,
                    event_count = pet_activity_windows.event_count + 1,
                    maximum_confidence = MAX(pet_activity_windows.maximum_confidence, excluded.maximum_confidence)
                """,
                (
                    event.application_id,
                    activity,
                    window_started_at.isoformat(),
                    occurred_at.isoformat(),
                    confidence,
                ),
            )
        return True

    def clear_activity_history(self) -> int:
        """The implementation behind the tray's one-click delete action."""

        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM pet_activity_events")
            connection.execute("DELETE FROM pet_activity_windows")
        return cursor.rowcount

    def activity_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM pet_activity_events").fetchone()[0])

    def list_activity_windows(self) -> list[ActivityWindow]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT application_id, activity, window_started_at, last_occurred_at,
                       event_count, maximum_confidence
                FROM pet_activity_windows
                ORDER BY window_started_at ASC, application_id ASC, activity ASC
                """
            ).fetchall()
        return [
            ActivityWindow(
                application_id=row["application_id"],
                activity=row["activity"],
                window_started_at=datetime.fromisoformat(row["window_started_at"]),
                last_occurred_at=datetime.fromisoformat(row["last_occurred_at"]),
                event_count=int(row["event_count"]),
                maximum_confidence=float(row["maximum_confidence"]),
            )
            for row in rows
        ]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pet_privacy_consent (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    activity_collection_enabled INTEGER NOT NULL CHECK (activity_collection_enabled IN (0, 1)),
                    remote_inference_enabled INTEGER NOT NULL CHECK (remote_inference_enabled IN (0, 1)),
                    decision_recorded INTEGER NOT NULL CHECK (decision_recorded IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS pet_activity_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    application_id TEXT NOT NULL,
                    idle_seconds INTEGER,
                    is_fullscreen INTEGER NOT NULL CHECK (is_fullscreen IN (0, 1)),
                    is_meeting_likely INTEGER NOT NULL CHECK (is_meeting_likely IN (0, 1)),
                    activity TEXT NOT NULL DEFAULT 'unknown',
                    confidence REAL NOT NULL DEFAULT 0,
                    fingerprint TEXT
                );
                CREATE TABLE IF NOT EXISTS pet_activity_windows (
                    application_id TEXT NOT NULL,
                    activity TEXT NOT NULL,
                    window_started_at TEXT NOT NULL,
                    last_occurred_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL CHECK (event_count > 0),
                    maximum_confidence REAL NOT NULL CHECK (maximum_confidence >= 0 AND maximum_confidence <= 1),
                    PRIMARY KEY (application_id, activity, window_started_at)
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(pet_privacy_consent)")}
            if "decision_recorded" not in columns:
                connection.execute(
                    "ALTER TABLE pet_privacy_consent ADD COLUMN decision_recorded INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (decision_recorded IN (0, 1))"
                )
            activity_columns = {row[1] for row in connection.execute("PRAGMA table_info(pet_activity_events)")}
            for name, definition in (
                ("activity", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("confidence", "REAL NOT NULL DEFAULT 0"),
                ("fingerprint", "TEXT"),
            ):
                if name not in activity_columns:
                    connection.execute(f"ALTER TABLE pet_activity_events ADD COLUMN {name} {definition}")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pet_activity_events_fingerprint "
                "ON pet_activity_events (fingerprint) WHERE fingerprint IS NOT NULL"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _activity_fingerprint(event: ActivityEvent, activity: str, window_started_at: datetime) -> str:
    payload = {
        "application_id": event.application_id,
        "source": event.source,
        "activity": activity,
        "metadata": {key: event.metadata[key] for key in sorted(event.metadata)},
        "window_started_at": window_started_at.isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _window_started_at(value: datetime) -> datetime:
    timestamp = int(value.timestamp())
    return datetime.fromtimestamp(timestamp - timestamp % _ACTIVITY_WINDOW_SECONDS, tz=timezone.utc)
