"""SQLite persistence for repository allowlist entries and audit events."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from refactor_agent.models import RepositoryAllowlistEventRecord, RepositoryAllowlistRecord


ConnectionFactory = Callable[[], sqlite3.Connection]


class SQLiteRepositoryAllowlistStore:
    """Persist dashboard-managed repository entries and their audit trail."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def add_repository_allowlist_entry(
        self,
        repo_full_name: str,
        max_entries: int | None = None,
    ) -> RepositoryAllowlistRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM repository_allowlist WHERE repo_full_name = ?",
                (repo_full_name,),
            ).fetchone()
            if existing is not None:
                return RepositoryAllowlistRecord(**dict(existing))
            if max_entries is not None:
                count_row = connection.execute(
                    "SELECT COUNT(*) AS count FROM repository_allowlist"
                ).fetchone()
                if int(count_row["count"]) >= max_entries:
                    return None
            created_at = _now()
            connection.execute(
                """
                INSERT INTO repository_allowlist (repo_full_name, created_at)
                VALUES (?, ?)
                """,
                (repo_full_name, created_at),
            )
            self._insert_event(connection, "ADD", repo_full_name)
            row = connection.execute(
                "SELECT * FROM repository_allowlist WHERE repo_full_name = ?",
                (repo_full_name,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Repository allowlist insert did not produce a record.")
        return RepositoryAllowlistRecord(**dict(row))

    def remove_repository_allowlist_entry(self, repo_full_name: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM repository_allowlist WHERE repo_full_name = ?",
                (repo_full_name,),
            )
            removed = bool(cursor.rowcount)
            if removed:
                self._insert_event(connection, "REMOVE", repo_full_name)
        return removed

    def get_repository_allowlist_entry(
        self,
        repo_full_name: str,
    ) -> RepositoryAllowlistRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM repository_allowlist WHERE repo_full_name = ?",
                (repo_full_name,),
            ).fetchone()
        return RepositoryAllowlistRecord(**dict(row)) if row else None

    def list_repository_allowlist_entries(self) -> list[RepositoryAllowlistRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM repository_allowlist ORDER BY repo_full_name"
            ).fetchall()
        return [RepositoryAllowlistRecord(**dict(row)) for row in rows]

    def count_repository_allowlist_entries(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM repository_allowlist").fetchone()
        return int(row["count"])

    def list_repository_allowlist_events(
        self,
        limit: int = 200,
    ) -> list[RepositoryAllowlistEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM repository_allowlist_events
                ORDER BY rowid LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [RepositoryAllowlistEventRecord(**dict(row)) for row in rows]

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        action: str,
        repo_full_name: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO repository_allowlist_events (
                event_id, action, repo_full_name, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (f"allowlist-event-{uuid4().hex}", action, repo_full_name, _now()),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
