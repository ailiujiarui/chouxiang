"""Local-only persistence for Nailong privacy consent and minimized activity."""

from __future__ import annotations

import sqlite3
import re
from pathlib import Path

from nailong_agent.events import ActivityEvent
from nailong_agent.privacy import PrivacyConsent

_PERSISTABLE_METADATA_KEYS = frozenset({"idle_seconds", "is_fullscreen", "is_meeting_likely"})
_APPLICATION_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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

    def append_minimized_activity(self, event: ActivityEvent) -> None:
        if (
            event.sensitivity != "public"
            or event.window_title_summary is not None
            or event.activity_hint is not None
            or not set(event.metadata).issubset(_PERSISTABLE_METADATA_KEYS)
            or not _APPLICATION_CATEGORY.fullmatch(event.application_id)
        ):
            raise ValueError("only minimized public activity events may be persisted")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO pet_activity_events
                    (event_id, occurred_at, source, application_id, idle_seconds, is_fullscreen, is_meeting_likely)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.occurred_at.isoformat(),
                    event.source,
                    event.application_id,
                    _nonnegative_int(event.metadata.get("idle_seconds")),
                    int(bool(event.metadata.get("is_fullscreen"))),
                    int(bool(event.metadata.get("is_meeting_likely"))),
                ),
            )

    def clear_activity_history(self) -> int:
        """The implementation behind the tray's one-click delete action."""

        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM pet_activity_events")
        return cursor.rowcount

    def activity_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM pet_activity_events").fetchone()[0])

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
                    is_meeting_likely INTEGER NOT NULL CHECK (is_meeting_likely IN (0, 1))
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(pet_privacy_consent)")}
            if "decision_recorded" not in columns:
                connection.execute(
                    "ALTER TABLE pet_privacy_consent ADD COLUMN decision_recorded INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (decision_recorded IN (0, 1))"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
