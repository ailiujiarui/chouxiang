"""Local-only persistence for Nailong privacy consent and minimized activity."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nailong_agent.events import ActivityEvent, ActivityType, ActivityWindow
from nailong_agent.privacy import PrivacyConsent

_APPLICATION_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LEGACY_WINDOW_SECONDS = 5 * 60


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
                (
                    int(consent.activity_collection_enabled),
                    int(consent.remote_inference_enabled),
                    int(consent.decision_recorded),
                ),
            )

    def append_minimized_activity(self, event: ActivityEvent) -> bool:
        if event.sensitivity != "public" or not _APPLICATION_CATEGORY.fullmatch(event.application_id):
            raise ValueError("only minimized public activity events may be persisted")
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO pet_activity_events
                    (event_id, occurred_at, source, application_id, idle_seconds,
                     is_fullscreen, is_meeting_likely, activity, confidence, summary)
                VALUES (?, ?, ?, ?, NULL, 0, 0, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.occurred_at.isoformat(),
                    event.source,
                    event.application_id,
                    event.activity.value,
                    event.confidence,
                    event.summary,
                ),
            ).rowcount
        return bool(inserted)

    def append_activity_window(self, window: ActivityWindow) -> bool:
        if window.sensitivity != "public":
            raise ValueError("only public activity windows may be persisted")
        window_key = _window_key(window)
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO pet_activity_windows
                    (window_key, window_started_at, window_ended_at,
                     dominant_application, dominant_activity, confidence,
                     event_count, duplicate_count, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_key,
                    window.window_started_at.isoformat(),
                    window.window_ended_at.isoformat(),
                    window.dominant_application,
                    window.dominant_activity.value,
                    window.confidence,
                    window.event_count,
                    window.duplicate_count,
                    window.summary,
                ),
            ).rowcount
        return bool(inserted)

    def clear_activity_history(self) -> int:
        """The implementation behind the tray's one-click delete action."""

        with self._connect() as connection:
            events = connection.execute("DELETE FROM pet_activity_events").rowcount
            windows = connection.execute("DELETE FROM pet_activity_windows").rowcount
        return events + windows

    def activity_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM pet_activity_events").fetchone()[0])

    def activity_window_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM pet_activity_windows").fetchone()[0])

    def list_activity_windows(self) -> list[ActivityWindow]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT window_started_at, window_ended_at, dominant_application,
                       dominant_activity, confidence, event_count, duplicate_count, summary
                FROM pet_activity_windows ORDER BY window_started_at
                """
            ).fetchall()
        return [
            ActivityWindow(
                window_started_at=row["window_started_at"],
                window_ended_at=row["window_ended_at"],
                dominant_application=row["dominant_application"],
                dominant_activity=ActivityType(row["dominant_activity"]),
                confidence=row["confidence"],
                event_count=row["event_count"],
                duplicate_count=row["duplicate_count"],
                summary=row["summary"],
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
                    confidence REAL NOT NULL DEFAULT 0.0,
                    summary TEXT
                );
                """
            )
            consent_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pet_privacy_consent)")
            }
            if "decision_recorded" not in consent_columns:
                connection.execute(
                    "ALTER TABLE pet_privacy_consent ADD COLUMN decision_recorded INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (decision_recorded IN (0, 1))"
                )
            event_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pet_activity_events)")
            }
            for name, definition in (
                ("activity", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("confidence", "REAL NOT NULL DEFAULT 0.0"),
                ("summary", "TEXT"),
            ):
                if name not in event_columns:
                    connection.execute(
                        f"ALTER TABLE pet_activity_events ADD COLUMN {name} {definition}"
                    )
            self._initialize_activity_windows(connection)

    def _initialize_activity_windows(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'pet_activity_windows'"
        ).fetchone()
        if existing is None:
            _create_activity_windows_table(connection)
            return

        columns = {row[1] for row in connection.execute("PRAGMA table_info(pet_activity_windows)")}
        if "window_key" in columns:
            return

        legacy_rows = connection.execute(
            """
            SELECT application_id, activity, window_started_at, last_occurred_at,
                   event_count, maximum_confidence
            FROM pet_activity_windows
            """
        ).fetchall()
        connection.execute("ALTER TABLE pet_activity_windows RENAME TO pet_activity_windows_legacy")
        _create_activity_windows_table(connection)
        for row in legacy_rows:
            started_at = _as_utc(datetime.fromisoformat(row["window_started_at"]))
            ended_at = started_at + timedelta(seconds=_LEGACY_WINDOW_SECONDS)
            activity = ActivityType(row["activity"])
            window = ActivityWindow(
                window_started_at=started_at,
                window_ended_at=ended_at,
                dominant_application=row["application_id"],
                dominant_activity=activity,
                confidence=row["maximum_confidence"],
                event_count=row["event_count"],
            )
            connection.execute(
                """
                INSERT INTO pet_activity_windows
                    (window_key, window_started_at, window_ended_at,
                     dominant_application, dominant_activity, confidence,
                     event_count, duplicate_count, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (
                    _window_key(window),
                    started_at.isoformat(),
                    ended_at.isoformat(),
                    window.dominant_application,
                    activity.value,
                    window.confidence,
                    window.event_count,
                ),
            )
        connection.execute("DROP TABLE pet_activity_windows_legacy")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _create_activity_windows_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE pet_activity_windows (
            window_key TEXT PRIMARY KEY,
            window_started_at TEXT NOT NULL,
            window_ended_at TEXT NOT NULL,
            dominant_application TEXT NOT NULL,
            dominant_activity TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
            event_count INTEGER NOT NULL CHECK(event_count >= 1),
            duplicate_count INTEGER NOT NULL CHECK(duplicate_count >= 0),
            summary TEXT
        )
        """
    )


def _window_key(window: ActivityWindow) -> str:
    return "|".join(
        (
            window.window_started_at.isoformat(),
            window.window_ended_at.isoformat(),
            window.dominant_application,
            window.dominant_activity.value,
        )
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
