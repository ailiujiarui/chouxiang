from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from refactor_agent.models import (
    GitHubAutomationResult,
    GitHubJobRecord,
    GitHubRefactorJob,
    RunRecord,
    TrajectoryMemoryRecord,
)


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

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def save_memory(self, record: TrajectoryMemoryRecord) -> None:
        created_at = record.created_at or _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO trajectory_memory (
                    memory_id, run_id, repo_name, target_path, status,
                    lesson, error_signature, reward, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        with self._connect() as connection:
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

    def create_github_job(self, job: GitHubRefactorJob) -> GitHubJobRecord:
        now = _now()
        record = GitHubJobRecord(
            job_id=job.job_id,
            delivery_id=job.delivery_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            target_path=job.target_path,
            tests_path=job.tests_path,
            status="QUEUED",
            payload_json=job.model_dump_json(),
            created_at=now,
            updated_at=now,
        )
        try:
            self.save_github_job(record)
        except sqlite3.IntegrityError:
            existing = self.get_github_job_by_delivery(job.delivery_id)
            if existing is None:
                existing = self.get_active_github_job(job.repo_full_name, job.issue_number)
            if existing is None:
                raise
            return existing
        return record

    def get_github_job_by_delivery(self, delivery_id: str) -> GitHubJobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM github_jobs WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return _job_record_from_row(row) if row else None

    def get_active_github_job(self, repo_full_name: str, issue_number: int) -> GitHubJobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM github_jobs
                WHERE repo_full_name = ? AND issue_number = ? AND status IN ('QUEUED', 'RUNNING')
                ORDER BY created_at ASC LIMIT 1
                """,
                (repo_full_name, issue_number),
            ).fetchone()
        return _job_record_from_row(row) if row else None

    def claim_next_github_job(
        self,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> GitHubJobRecord | None:
        now = _now()
        lease_expires = _now(timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE github_jobs
                SET status = 'FAILED', error = 'worker lease expired after retry limit',
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE status = 'RUNNING' AND lease_expires_at < ? AND attempt_count >= ?
                """,
                (now, now, max_attempts),
            )
            connection.execute(
                """
                UPDATE github_jobs
                SET status = 'QUEUED', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE status = 'RUNNING' AND lease_expires_at < ? AND attempt_count < ?
                """,
                (now, now, max_attempts),
            )
            row = connection.execute(
                """
                SELECT * FROM github_jobs
                WHERE status = 'QUEUED' AND attempt_count < ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (max_attempts,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE github_jobs
                SET status = 'RUNNING', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'QUEUED'
                """,
                (worker_id, lease_expires, now, row["job_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
        return _job_record_from_row(claimed) if claimed else None

    def mark_github_job_running(self, job_id: str) -> None:
        existing = self.get_github_job(job_id)
        if existing is None:
            return
        existing.status = "RUNNING"
        existing.updated_at = _now()
        self.save_github_job(existing)

    def renew_github_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE github_jobs SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (_now(timedelta(seconds=lease_seconds)), _now(), job_id, worker_id),
            )
        return cursor.rowcount == 1

    def mark_github_job_failed(self, job_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE github_jobs
                SET status = 'FAILED', error = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (error, _now(), job_id),
            )

    def complete_github_job(self, job: GitHubRefactorJob, result: GitHubAutomationResult) -> GitHubJobRecord:
        existing = self.get_github_job(job.job_id)
        now = _now()
        record = GitHubJobRecord(
            job_id=job.job_id,
            delivery_id=job.delivery_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            target_path=job.target_path,
            tests_path=job.tests_path,
            status=result.status,
            branch_name=result.branch_name,
            run_id=result.run_id,
            pr_url=result.pr_url,
            workspace_path=result.workspace_path,
            error=result.error,
            payload_json=existing.payload_json if existing else job.model_dump_json(),
            attempt_count=existing.attempt_count if existing else 0,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.save_github_job(record)
        return record

    def fail_github_job(self, job: GitHubRefactorJob, error: str) -> GitHubJobRecord:
        result = GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            status="FAILED",
            error=error,
        )
        return self.complete_github_job(job, result)

    def save_github_job(self, record: GitHubJobRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO github_jobs (
                    job_id, delivery_id, repo_full_name, issue_number, target_path, tests_path, status,
                    branch_name, run_id, pr_url, workspace_path, error, payload_json, attempt_count,
                    lease_owner, lease_expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    delivery_id=excluded.delivery_id,
                    repo_full_name=excluded.repo_full_name,
                    issue_number=excluded.issue_number,
                    target_path=excluded.target_path,
                    tests_path=excluded.tests_path,
                    status=excluded.status,
                    branch_name=excluded.branch_name,
                    run_id=excluded.run_id,
                    pr_url=excluded.pr_url,
                    workspace_path=excluded.workspace_path,
                    error=excluded.error,
                    payload_json=excluded.payload_json,
                    attempt_count=excluded.attempt_count,
                    lease_owner=excluded.lease_owner,
                    lease_expires_at=excluded.lease_expires_at,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                (
                    record.job_id,
                    record.delivery_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.target_path,
                    record.tests_path,
                    record.status,
                    record.branch_name,
                    record.run_id,
                    record.pr_url,
                    str(record.workspace_path) if record.workspace_path else None,
                    record.error,
                    record.payload_json,
                    record.attempt_count,
                    record.lease_owner,
                    record.lease_expires_at,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def get_github_job(self, job_id: str) -> GitHubJobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM github_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _job_record_from_row(row) if row else None

    def list_github_jobs(self, limit: int = 20) -> list[GitHubJobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM github_jobs ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_job_record_from_row(row) for row in rows]

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS github_jobs (
                    job_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL UNIQUE,
                    repo_full_name TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    target_path TEXT NOT NULL,
                    tests_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'DRY_RUN')),
                    branch_name TEXT,
                    run_id TEXT,
                    pr_url TEXT,
                    workspace_path TEXT,
                    error TEXT,
                    payload_json TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trajectory_memory (
                    memory_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    repo_name TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
                    lesson TEXT NOT NULL,
                    error_signature TEXT,
                    reward REAL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trajectory_memory_lookup
                ON trajectory_memory (repo_name, target_path, created_at DESC)
                """
            )
            _migrate_github_jobs(connection)


def _now(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _job_record_from_row(row: sqlite3.Row) -> GitHubJobRecord:
    data = dict(row)
    if data.get("workspace_path"):
        data["workspace_path"] = Path(data["workspace_path"])
    return GitHubJobRecord(**data)


def _migrate_github_jobs(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(github_jobs)").fetchall()}
    additions = {
        "delivery_id": "TEXT",
        "payload_json": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "lease_owner": "TEXT",
        "lease_expires_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE github_jobs ADD COLUMN {name} {definition}")
    connection.execute("UPDATE github_jobs SET delivery_id = job_id WHERE delivery_id IS NULL")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_github_jobs_delivery ON github_jobs (delivery_id)"
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_github_jobs_active_issue
        ON github_jobs (repo_full_name, issue_number)
        WHERE status IN ('QUEUED', 'RUNNING')
        """
    )
