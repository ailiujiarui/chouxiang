from __future__ import annotations

import sqlite3
from pathlib import Path

from refactor_agent.models import RunRecord


class SQLiteRunStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def save(self, record: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, issue_id, repo_name, pre_loc, post_loc, pre_cc, post_cc,
                    self_heal_count, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.issue_id,
                    record.repo_name,
                    record.pre_loc,
                    record.post_loc,
                    record.pre_cc,
                    record.post_cc,
                    record.self_heal_count,
                    record.status,
                    record.error,
                ),
            )

    def get(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunRecord(**dict(row))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    issue_id TEXT,
                    repo_name TEXT NOT NULL,
                    pre_loc INTEGER,
                    post_loc INTEGER,
                    pre_cc INTEGER,
                    post_cc INTEGER,
                    self_heal_count INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
                    error TEXT
                )
                """
            )
