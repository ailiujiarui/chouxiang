"""SQLite persistence for public analysis events and task lifecycle projections."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType, PublishReceipt
from refactor_agent.models import GitHubJobStatus


ConnectionFactory = Callable[[], sqlite3.Connection]


class SQLiteAnalysisEventStore:
    """Persist analysis events without owning or sharing SQLite connections."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def emit(self, event: AnalysisEvent) -> PublishReceipt:
        """Persist a sanitized analysis event with idempotent event IDs."""

        try:
            with self._connect() as connection:
                sequence = self.insert_event(connection, event)
        except sqlite3.IntegrityError:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT sequence FROM analysis_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
            return PublishReceipt(
                accepted=True,
                duplicate=True,
                sequence=int(row["sequence"]) if row else None,
                reason="duplicate_event",
            )
        return PublishReceipt(accepted=True, sequence=sequence, reason="persisted")

    def list_analysis_events(self, *, after: int = 0, limit: int = 100) -> list[AnalysisEvent]:
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_events
                WHERE sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (max(after, 0), bounded_limit),
            ).fetchall()
        return [_analysis_event_from_row(row) for row in rows]

    def read_public_analysis_event_page(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[list[AnalysisEvent], int, int, bool]:
        """Read a public-only page against one captured sequence high-water mark."""

        bounded_after = max(after, 0)
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest FROM analysis_events"
            ).fetchone()
            latest_sequence = int(row["latest"] if row else 0)
            rows = connection.execute(
                """
                SELECT * FROM analysis_events
                WHERE sequence > ? AND sequence <= ? AND sensitivity = 'public'
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (bounded_after, latest_sequence, bounded_limit),
            ).fetchall()
            events = [_analysis_event_from_row(event_row) for event_row in rows]
            next_sequence = int(events[-1].sequence) if events else latest_sequence
            has_more = bool(
                connection.execute(
                    """
                    SELECT 1 FROM analysis_events
                    WHERE sequence > ? AND sequence <= ? AND sensitivity = 'public'
                    LIMIT 1
                    """,
                    (next_sequence, latest_sequence),
                ).fetchone()
            )
        return events, next_sequence, latest_sequence, has_more

    def latest_analysis_event_sequence(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest FROM analysis_events"
            ).fetchone()
        return int(row["latest"] if row else 0)

    def prune_analysis_events(self, *, older_than: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM analysis_events WHERE occurred_at < ?",
                (older_than.astimezone(timezone.utc).isoformat(),),
            )
        return cursor.rowcount

    def insert_event(
        self,
        connection: sqlite3.Connection,
        event: AnalysisEvent,
    ) -> int:
        """Insert an event using the caller's transaction connection."""

        cursor = connection.execute(
            """
            INSERT INTO analysis_events (
                event_id, schema_version, event_type, task_id, run_id, source,
                phase, attempt, evidence_level, error_category, recoverable,
                deadline_at, safe_metrics_json, occurred_at, sensitivity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.schema_version,
                event.event_type.value,
                event.task_id,
                event.run_id,
                event.source,
                event.phase,
                event.attempt,
                event.evidence_level,
                event.error_category,
                int(event.recoverable) if event.recoverable is not None else None,
                event.deadline_at.astimezone(timezone.utc).isoformat() if event.deadline_at else None,
                json.dumps(event.safe_metrics, ensure_ascii=False, sort_keys=True),
                event.occurred_at.astimezone(timezone.utc).isoformat(),
                event.sensitivity,
            ),
        )
        return int(cursor.lastrowid)

    def insert_task_event(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        status: GitHubJobStatus,
        attempt: int,
        run_id: str | None = None,
        deadline_at: str | None = None,
    ) -> None:
        """Project a task status using the caller's existing job transaction."""

        event_types = {
            GitHubJobStatus.QUEUED: AnalysisEventType.TASK_QUEUED,
            GitHubJobStatus.RUNNING: AnalysisEventType.TASK_STARTED,
            GitHubJobStatus.SUCCESS: AnalysisEventType.TASK_COMPLETED,
            GitHubJobStatus.DRY_RUN: AnalysisEventType.TASK_COMPLETED,
            GitHubJobStatus.FAILED: AnalysisEventType.TASK_FAILED,
            GitHubJobStatus.TIMED_OUT: AnalysisEventType.TASK_TIMED_OUT,
            GitHubJobStatus.CANCELLED: AnalysisEventType.TASK_CANCELLED,
        }
        event_type = event_types.get(status)
        if event_type is None:
            return
        self.insert_event(
            connection,
            AnalysisEvent(
                event_type=event_type,
                task_id=job_id,
                run_id=run_id,
                source="worker",
                attempt=attempt,
                deadline_at=datetime.fromisoformat(deadline_at) if deadline_at else None,
                safe_metrics={"job_status": status.value},
            ),
        )


def _analysis_event_from_row(row: sqlite3.Row) -> AnalysisEvent:
    data = dict(row)
    data["safe_metrics"] = json.loads(data.pop("safe_metrics_json") or "{}")
    if data.get("recoverable") is not None:
        data["recoverable"] = bool(data["recoverable"])
    return AnalysisEvent.model_validate(data)
