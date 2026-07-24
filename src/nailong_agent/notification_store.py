from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from nailong_agent.events import (
    NotificationIngestReceipt,
    NotificationIntent,
    NotificationKind,
    NotificationStatus,
)
from nailong_agent.notification_policy import NotificationCandidate
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType


_TASK_TERMINALS = {
    AnalysisEventType.FINAL_VERDICT_PASSED,
    AnalysisEventType.FINAL_VERDICT_FAILED,
    AnalysisEventType.TASK_COMPLETED,
    AnalysisEventType.TASK_FAILED,
    AnalysisEventType.TASK_TIMED_OUT,
    AnalysisEventType.TASK_CANCELLED,
}


class NotificationStore:
    """Durable desktop-side inbox, policy state, cursor, and acknowledgement store."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def process_event(
        self,
        event: AnalysisEvent,
        candidate: NotificationCandidate | None,
        *,
        now: datetime,
        cooldown_seconds: int,
    ) -> NotificationIngestReceipt:
        now = _as_utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO consumed_analysis_events (event_id, sequence, consumed_at) VALUES (?, ?, ?)",
                (event.event_id, event.sequence, now.isoformat()),
            ).rowcount
            self._advance_cursor(connection, event.sequence)
            if not inserted:
                return NotificationIngestReceipt(accepted=True, duplicate=True, reason="duplicate_event")
            if event.sensitivity != "public":
                return NotificationIngestReceipt(accepted=False, reason="non_public_event")

            self._update_task_state(connection, event)
            if candidate is None:
                return NotificationIngestReceipt(accepted=True, reason="no_notification_for_event")
            if candidate.terminal:
                return self._process_terminal_candidate(connection, event, candidate, now)

            runtime = self._runtime_row(connection)
            if bool(runtime["do_not_disturb"]):
                return NotificationIngestReceipt(accepted=True, reason="do_not_disturb")
            next_regular_at = _parse_datetime(runtime["next_regular_at"])
            if next_regular_at is not None and now < next_regular_at:
                return NotificationIngestReceipt(accepted=True, reason="regular_cooldown")
            intent = self._insert_intent(
                connection,
                task_id=event.task_id,
                candidate=candidate,
                source_event_id=event.event_id,
                dedupe_key=f"event:{event.event_id}",
                now=now,
            )
            connection.execute(
                "UPDATE notification_runtime SET next_regular_at = ? WHERE id = 1",
                ((now + timedelta(seconds=cooldown_seconds)).isoformat(),),
            )
            return NotificationIngestReceipt(
                accepted=True,
                notification_id=intent.notification_id,
                reason="notification_enqueued",
            )

    def enqueue_due_long_task(
        self,
        candidate: NotificationCandidate,
        *,
        now: datetime,
        cooldown_seconds: int,
    ) -> NotificationIntent | None:
        now = _as_utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runtime = self._runtime_row(connection)
            if bool(runtime["do_not_disturb"]):
                return None
            next_regular_at = _parse_datetime(runtime["next_regular_at"])
            if next_regular_at is not None and now < next_regular_at:
                return None
            task = connection.execute(
                """
                SELECT task_id, started_at FROM notification_tasks
                WHERE active = 1 AND long_reminder_sent = 0
                  AND reminder_at IS NOT NULL AND reminder_at <= ?
                ORDER BY reminder_at ASC, task_id ASC
                LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if task is None:
                return None
            intent = self._insert_intent(
                connection,
                task_id=task["task_id"],
                candidate=candidate,
                source_event_id=None,
                dedupe_key=f"long-task:{task['task_id']}:{task['started_at']}",
                now=now,
            )
            connection.execute(
                "UPDATE notification_tasks SET long_reminder_sent = 1 WHERE task_id = ?",
                (task["task_id"],),
            )
            connection.execute(
                "UPDATE notification_runtime SET next_regular_at = ? WHERE id = 1",
                ((now + timedelta(seconds=cooldown_seconds)).isoformat(),),
            )
        return intent

    def set_do_not_disturb(
        self,
        enabled: bool,
        *,
        now: datetime,
        summary_factory: Callable[[int], NotificationCandidate] | None = None,
    ) -> NotificationIntent | None:
        now = _as_utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runtime = self._runtime_row(connection)
            if bool(runtime["do_not_disturb"]) == enabled:
                return None
            summary: NotificationIntent | None = None
            if enabled:
                pending_terminals = connection.execute(
                    """
                    SELECT task_id, kind FROM notification_intents
                    WHERE status = 'PENDING' AND terminal = 1 AND kind != ?
                    """,
                    (NotificationKind.QUIET_MODE_SUMMARY.value,),
                ).fetchall()
                for row in pending_terminals:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO suppressed_terminal_tasks (task_id, kind, occurred_at)
                        VALUES (?, ?, ?)
                        """,
                        (row["task_id"], row["kind"], now.isoformat()),
                    )
                connection.execute(
                    "UPDATE notification_intents SET status = 'DROPPED' WHERE status = 'PENDING'"
                )
            else:
                count = int(
                    connection.execute("SELECT COUNT(*) FROM suppressed_terminal_tasks").fetchone()[0]
                )
                if count and summary_factory is not None:
                    summary = self._insert_intent(
                        connection,
                        task_id="quiet-mode-summary",
                        candidate=summary_factory(count),
                        source_event_id=None,
                        dedupe_key=f"quiet-summary:{uuid4().hex}",
                        now=now,
                    )
                connection.execute("DELETE FROM suppressed_terminal_tasks")
            connection.execute(
                "UPDATE notification_runtime SET do_not_disturb = ? WHERE id = 1",
                (int(enabled),),
            )
        return summary

    def suppressed_terminal_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM suppressed_terminal_tasks").fetchone()[0])

    def lease_next_intent(
        self,
        *,
        now: datetime,
        minimum_start_spacing_seconds: int = 30,
    ) -> NotificationIntent | None:
        now = _as_utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runtime = self._runtime_row(connection)
            if bool(runtime["do_not_disturb"]):
                return None
            last_started = _parse_datetime(runtime["last_popup_started_at"])
            if last_started is not None and now < last_started + timedelta(seconds=minimum_start_spacing_seconds):
                return None
            row = connection.execute(
                """
                SELECT * FROM notification_intents
                WHERE status = 'PENDING' AND available_at <= ?
                ORDER BY terminal DESC,
                    CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                    created_at ASC
                LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE notification_intents SET status = 'DISPLAYING' WHERE notification_id = ?",
                (row["notification_id"],),
            )
            connection.execute(
                "UPDATE notification_runtime SET last_popup_started_at = ? WHERE id = 1",
                (now.isoformat(),),
            )
        return _intent_from_row(row)

    def acknowledge(self, notification_id: str, outcome: str, *, now: datetime) -> bool:
        statuses = {"shown": "SHOWN", "dismissed": "DISMISSED", "failed": "FAILED"}
        if outcome not in statuses:
            raise ValueError("unsupported notification acknowledgement")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_intents SET status = ?, acknowledged_at = ?
                WHERE notification_id = ? AND status = 'DISPLAYING'
                """,
                (statuses[outcome], _as_utc(now).isoformat(), notification_id),
            )
        return cursor.rowcount == 1

    def status(self) -> NotificationStatus:
        with self._connect() as connection:
            runtime = self._runtime_row(connection)
            pending_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM notification_intents WHERE status IN ('PENDING', 'DISPLAYING')"
                ).fetchone()[0]
            )
            suppressed_count = int(
                connection.execute("SELECT COUNT(*) FROM suppressed_terminal_tasks").fetchone()[0]
            )
        return NotificationStatus(
            do_not_disturb=bool(runtime["do_not_disturb"]),
            last_consumed_sequence=int(runtime["last_consumed_sequence"]),
            next_regular_at=_parse_datetime(runtime["next_regular_at"]),
            last_popup_started_at=_parse_datetime(runtime["last_popup_started_at"]),
            pending_count=pending_count,
            suppressed_terminal_count=suppressed_count,
        )

    def list_intents(self) -> list[NotificationIntent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notification_intents ORDER BY created_at ASC, notification_id ASC"
            ).fetchall()
        return [_intent_from_row(row) for row in rows]

    def _process_terminal_candidate(
        self,
        connection: sqlite3.Connection,
        event: AnalysisEvent,
        candidate: NotificationCandidate,
        now: datetime,
    ) -> NotificationIngestReceipt:
        task = connection.execute(
            "SELECT terminal_seen FROM notification_tasks WHERE task_id = ?",
            (event.task_id,),
        ).fetchone()
        if task is not None and bool(task["terminal_seen"]):
            return NotificationIngestReceipt(accepted=True, reason="terminal_already_recorded")
        connection.execute(
            """
            INSERT INTO notification_tasks (task_id, terminal_seen, active, long_reminder_sent)
            VALUES (?, 1, 0, 0)
            ON CONFLICT(task_id) DO UPDATE SET terminal_seen = 1
            """,
            (event.task_id,),
        )
        if bool(self._runtime_row(connection)["do_not_disturb"]):
            connection.execute(
                """
                INSERT OR IGNORE INTO suppressed_terminal_tasks (task_id, kind, occurred_at)
                VALUES (?, ?, ?)
                """,
                (event.task_id, candidate.kind.value, now.isoformat()),
            )
            return NotificationIngestReceipt(accepted=True, reason="terminal_summarized_during_dnd")
        intent = self._insert_intent(
            connection,
            task_id=event.task_id,
            candidate=candidate,
            source_event_id=event.event_id,
            dedupe_key=f"terminal:{event.task_id}:{event.event_id}",
            now=now,
        )
        return NotificationIngestReceipt(
            accepted=True,
            notification_id=intent.notification_id,
            reason="terminal_notification_enqueued",
        )

    def _update_task_state(self, connection: sqlite3.Connection, event: AnalysisEvent) -> None:
        if event.event_type == AnalysisEventType.TASK_QUEUED:
            connection.execute(
                """
                INSERT INTO notification_tasks (task_id, active, long_reminder_sent, terminal_seen)
                VALUES (?, 0, 0, 0)
                ON CONFLICT(task_id) DO UPDATE SET
                    active = 0, long_reminder_sent = 0, terminal_seen = 0,
                    started_at = NULL, deadline_at = NULL, reminder_at = NULL
                """,
                (event.task_id,),
            )
        elif event.event_type == AnalysisEventType.TASK_STARTED:
            started_at = _as_utc(event.occurred_at)
            deadline_at = _as_utc(event.deadline_at) if event.deadline_at else None
            reminder_at = None
            if deadline_at is not None and deadline_at > started_at:
                reminder_at = started_at + (deadline_at - started_at) / 3
            connection.execute(
                """
                INSERT INTO notification_tasks (
                    task_id, run_id, started_at, deadline_at, reminder_at,
                    active, long_reminder_sent, terminal_seen
                ) VALUES (?, ?, ?, ?, ?, 1, 0, 0)
                ON CONFLICT(task_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    started_at = excluded.started_at,
                    deadline_at = excluded.deadline_at,
                    reminder_at = excluded.reminder_at,
                    active = 1,
                    long_reminder_sent = 0,
                    terminal_seen = 0
                """,
                (
                    event.task_id,
                    event.run_id,
                    started_at.isoformat(),
                    deadline_at.isoformat() if deadline_at else None,
                    reminder_at.isoformat() if reminder_at else None,
                ),
            )
        elif event.event_type in _TASK_TERMINALS:
            connection.execute(
                "UPDATE notification_tasks SET active = 0 WHERE task_id = ?",
                (event.task_id,),
            )

    def _insert_intent(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        candidate: NotificationCandidate,
        source_event_id: str | None,
        dedupe_key: str,
        now: datetime,
    ) -> NotificationIntent:
        intent = NotificationIntent(
            task_id=task_id,
            kind=candidate.kind,
            message=candidate.message,
            priority=candidate.priority,
            terminal=candidate.terminal,
            dedupe_key=dedupe_key,
            source_event_id=source_event_id,
            created_at=now,
            available_at=now,
        )
        connection.execute(
            """
            INSERT INTO notification_intents (
                notification_id, task_id, kind, message, priority, terminal,
                dedupe_key, source_event_id, created_at, available_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (
                intent.notification_id,
                intent.task_id,
                intent.kind.value,
                intent.message,
                intent.priority,
                int(intent.terminal),
                intent.dedupe_key,
                intent.source_event_id,
                intent.created_at.isoformat(),
                intent.available_at.isoformat(),
            ),
        )
        return intent

    @staticmethod
    def _advance_cursor(connection: sqlite3.Connection, sequence: int | None) -> None:
        if sequence is not None:
            connection.execute(
                """
                UPDATE notification_runtime
                SET last_consumed_sequence = MAX(last_consumed_sequence, ?)
                WHERE id = 1
                """,
                (sequence,),
            )

    @staticmethod
    def _runtime_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM notification_runtime WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("notification runtime row is missing")
        return row

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notification_runtime (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    do_not_disturb INTEGER NOT NULL DEFAULT 0 CHECK(do_not_disturb IN (0, 1)),
                    last_consumed_sequence INTEGER NOT NULL DEFAULT 0,
                    next_regular_at TEXT,
                    last_popup_started_at TEXT
                );
                INSERT OR IGNORE INTO notification_runtime (id) VALUES (1);

                CREATE TABLE IF NOT EXISTS consumed_analysis_events (
                    event_id TEXT PRIMARY KEY,
                    sequence INTEGER,
                    consumed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    started_at TEXT,
                    deadline_at TEXT,
                    reminder_at TEXT,
                    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
                    long_reminder_sent INTEGER NOT NULL DEFAULT 0 CHECK(long_reminder_sent IN (0, 1)),
                    terminal_seen INTEGER NOT NULL DEFAULT 0 CHECK(terminal_seen IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS suppressed_terminal_tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_intents (
                    notification_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    priority TEXT NOT NULL CHECK(priority IN ('low', 'normal', 'high')),
                    terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
                    dedupe_key TEXT NOT NULL UNIQUE,
                    source_event_id TEXT,
                    created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'PENDING', 'DISPLAYING', 'SHOWN', 'DISMISSED', 'FAILED', 'DROPPED'
                    ))
                );
                CREATE INDEX IF NOT EXISTS idx_notification_intents_delivery
                ON notification_intents (status, terminal, available_at, created_at);
                """
            )
            connection.execute(
                "UPDATE notification_intents SET status = 'PENDING' WHERE status = 'DISPLAYING'"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _intent_from_row(row: sqlite3.Row) -> NotificationIntent:
    return NotificationIntent(
        notification_id=row["notification_id"],
        task_id=row["task_id"],
        kind=NotificationKind(row["kind"]),
        message=row["message"],
        priority=row["priority"],
        terminal=bool(row["terminal"]),
        dedupe_key=row["dedupe_key"],
        source_event_id=row["source_event_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        available_at=datetime.fromisoformat(row["available_at"]),
    )


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
