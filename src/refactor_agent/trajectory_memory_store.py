"""SQLite persistence for reusable refactoring trajectory memories."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from refactor_agent.models import TrajectoryMemoryRecord


ConnectionFactory = Callable[[], sqlite3.Connection]


class SQLiteTrajectoryMemoryStore:
    """Own trajectory memory records without deciding how lessons are produced."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def save_memory(self, record: TrajectoryMemoryRecord) -> None:
        """Upsert an independently owned memory record without delete-and-reinsert effects."""

        created_at = record.created_at or _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO trajectory_memory (
                    memory_id, run_id, repo_name, target_path, status,
                    lesson, error_signature, reward, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    repo_name = excluded.repo_name,
                    target_path = excluded.target_path,
                    status = excluded.status,
                    lesson = excluded.lesson,
                    error_signature = excluded.error_signature,
                    reward = excluded.reward,
                    created_at = excluded.created_at
                """,
                (
                    record.memory_id,
                    record.run_id,
                    record.repo_name,
                    record.target_path,
                    record.status,
                    record.lesson,
                    record.error_signature,
                    record.reward,
                    created_at,
                ),
            )

    def list_memory(
        self,
        repo_name: str | None = None,
        target_path: str | None = None,
        limit: int = 5,
    ) -> list[TrajectoryMemoryRecord]:
        clauses = []
        params: list[object] = []
        if repo_name:
            clauses.append("repo_name = ?")
            params.append(repo_name)
        if target_path:
            clauses.append("target_path = ?")
            params.append(target_path)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM trajectory_memory
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [TrajectoryMemoryRecord(**dict(row)) for row in rows]

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back a fresh operation connection, then always close it."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
