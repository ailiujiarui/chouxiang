from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from refactor_agent.models import GitHubAutomationResult, GitHubJobRecord, GitHubRefactorJob, RunRecord


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

    def create_github_job(self, job: GitHubRefactorJob) -> GitHubJobRecord:
        now = _now()
        record = GitHubJobRecord(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            target_path=job.target_path,
            tests_path=job.tests_path,
            status="QUEUED",
            created_at=now,
            updated_at=now,
        )
        self.save_github_job(record)
        return record

    def mark_github_job_running(self, job_id: str) -> None:
        existing = self.get_github_job(job_id)
        if existing is None:
            return
        existing.status = "RUNNING"
        existing.updated_at = _now()
        self.save_github_job(existing)

    def complete_github_job(self, job: GitHubRefactorJob, result: GitHubAutomationResult) -> GitHubJobRecord:
        existing = self.get_github_job(job.job_id)
        now = _now()
        record = GitHubJobRecord(
            job_id=job.job_id,
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
                INSERT OR REPLACE INTO github_jobs (
                    job_id, repo_full_name, issue_number, target_path, tests_path, status,
                    branch_name, run_id, pr_url, workspace_path, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.job_id,
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_record_from_row(row: sqlite3.Row) -> GitHubJobRecord:
    data = dict(row)
    if data.get("workspace_path"):
        data["workspace_path"] = Path(data["workspace_path"])
    return GitHubJobRecord(**data)
