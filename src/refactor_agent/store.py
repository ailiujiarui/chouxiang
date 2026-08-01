from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from refactor_agent.analysis_event_store import SQLiteAnalysisEventStore
from refactor_agent.analysis_events import AnalysisEvent, PublishReceipt
from refactor_agent.github_job_store import JobTransitionError, SQLiteGitHubJobStore
from refactor_agent.models import (
    BenchmarkCaseRecord,
    BenchmarkRunRecord,
    GitHubAutomationResult,
    GitHubJobRecord,
    GitHubJobStatus,
    GitHubRefactorJob,
    JobEventRecord,
    RepositoryAllowlistEventRecord,
    RepositoryAllowlistRecord,
    RunRecord,
    TrajectoryMemoryRecord,
)
from refactor_agent.repository_allowlist_store import SQLiteRepositoryAllowlistStore
from refactor_agent.run_record_store import SQLiteRunRecordStore
from refactor_agent.sqlite_runtime import (
    SQLiteDiagnostics,
    SQLitePolicy,
    connect_sqlite,
    initialize_sqlite_database,
    log_sqlite_diagnostics,
)
from refactor_agent.store_schema import ensure_main_schema


class SQLiteRunStore:
    """Main durable Store with shared SQLite policy and conditional state writes."""

    def __init__(self, database_path: Path, policy: SQLitePolicy | None = None) -> None:
        """Negotiate SQLite once, record safe diagnostics, then initialize the schema."""

        self.database_path = database_path
        self.sqlite_policy = policy or SQLitePolicy.from_env()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite_diagnostics: SQLiteDiagnostics = initialize_sqlite_database(
            self.database_path,
            self.sqlite_policy,
        )
        log_sqlite_diagnostics("main", self.sqlite_diagnostics)
        self._ensure_schema()
        self._analysis_events = SQLiteAnalysisEventStore(self._connect)
        self._run_records = SQLiteRunRecordStore(self._connect)
        self._github_jobs = SQLiteGitHubJobStore(self._connect, self._analysis_events)
        self._repository_allowlist = SQLiteRepositoryAllowlistStore(self._connect)

    def emit(self, event: AnalysisEvent) -> PublishReceipt:
        return self._analysis_events.emit(event)

    def list_analysis_events(self, *, after: int = 0, limit: int = 100) -> list[AnalysisEvent]:
        return self._analysis_events.list_analysis_events(after=after, limit=limit)

    def read_public_analysis_event_page(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[list[AnalysisEvent], int, int, bool]:
        return self._analysis_events.read_public_analysis_event_page(after=after, limit=limit)

    def latest_analysis_event_sequence(self) -> int:
        return self._analysis_events.latest_analysis_event_sequence()

    def prune_analysis_events(self, *, older_than: datetime) -> int:
        return self._analysis_events.prune_analysis_events(older_than=older_than)

    def save(self, record: RunRecord) -> None:
        self._run_records.save(record)

    def get(self, run_id: str) -> RunRecord | None:
        return self._run_records.get(run_id)

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        return self._run_records.list_runs(limit)

    def save_benchmark_run(
        self,
        run: BenchmarkRunRecord,
        cases: list[BenchmarkCaseRecord],
    ) -> None:
        self._run_records.save_benchmark_run(run, cases)

    def get_benchmark_run(self, run_id: str) -> BenchmarkRunRecord | None:
        return self._run_records.get_benchmark_run(run_id)

    def list_benchmark_runs(self, limit: int = 20) -> list[BenchmarkRunRecord]:
        return self._run_records.list_benchmark_runs(limit)

    def list_benchmark_case_results(self, run_id: str) -> list[BenchmarkCaseRecord]:
        return self._run_records.list_benchmark_case_results(run_id)

    def save_memory(self, record: TrajectoryMemoryRecord) -> None:
        """Upsert an independently owned memory record without delete-and-reinsert effects."""

        created_at = record.created_at or _now()
        with self._connect() as connection:
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
        return self._github_jobs.create_github_job(job)

    def get_github_job_by_delivery(self, delivery_id: str) -> GitHubJobRecord | None:
        return self._github_jobs.get_github_job_by_delivery(delivery_id)

    def get_active_github_job(self, repo_full_name: str, issue_number: int) -> GitHubJobRecord | None:
        return self._github_jobs.get_active_github_job(repo_full_name, issue_number)

    def claim_next_github_job(
        self,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int = 900,
    ) -> GitHubJobRecord | None:
        return self._github_jobs.claim_next_github_job(
            worker_id,
            lease_seconds,
            max_attempts,
            deadline_seconds,
        )

    def mark_github_job_running(self, job_id: str) -> None:
        self._github_jobs.mark_github_job_running(job_id)

    def transition_github_job(
        self,
        job_id: str,
        to_status: GitHubJobStatus | str,
        *,
        worker_id: str | None = None,
        message: str = "",
        require_owner: bool = False,
    ) -> GitHubJobRecord:
        return self._github_jobs.transition_github_job(
            job_id,
            to_status,
            worker_id=worker_id,
            message=message,
            require_owner=require_owner,
        )

    def request_github_job_cancellation(self, job_id: str) -> tuple[GitHubJobRecord, bool]:
        return self._github_jobs.request_github_job_cancellation(job_id)

    def retry_github_job(self, job_id: str) -> GitHubJobRecord:
        return self._github_jobs.retry_github_job(job_id)

    def list_job_events(self, job_id: str, limit: int = 200) -> list[JobEventRecord]:
        return self._github_jobs.list_job_events(job_id, limit)

    def renew_github_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        return self._github_jobs.renew_github_job_lease(job_id, worker_id, lease_seconds)

    def mark_github_job_failed(
        self,
        job_id: str,
        error: str,
        worker_id: str | None = None,
    ) -> None:
        self._github_jobs.mark_github_job_failed(job_id, error, worker_id)

    def mark_github_job_timed_out(self, job_id: str, error: str, worker_id: str) -> None:
        self._github_jobs.mark_github_job_timed_out(job_id, error, worker_id)

    def complete_github_job(
        self,
        job: GitHubRefactorJob,
        result: GitHubAutomationResult,
        worker_id: str | None = None,
    ) -> GitHubJobRecord:
        return self._github_jobs.complete_github_job(job, result, worker_id)

    def fail_github_job(
        self,
        job: GitHubRefactorJob,
        error: str,
        worker_id: str | None = None,
    ) -> GitHubJobRecord:
        return self._github_jobs.fail_github_job(job, error, worker_id)

    def get_github_job(self, job_id: str) -> GitHubJobRecord | None:
        return self._github_jobs.get_github_job(job_id)

    def list_github_jobs(self, limit: int = 20) -> list[GitHubJobRecord]:
        return self._github_jobs.list_github_jobs(limit)

    def add_repository_allowlist_entry(
        self,
        repo_full_name: str,
        max_entries: int | None = None,
    ) -> RepositoryAllowlistRecord | None:
        return self._repository_allowlist.add_repository_allowlist_entry(
            repo_full_name,
            max_entries=max_entries,
        )

    def remove_repository_allowlist_entry(self, repo_full_name: str) -> bool:
        return self._repository_allowlist.remove_repository_allowlist_entry(repo_full_name)

    def get_repository_allowlist_entry(self, repo_full_name: str) -> RepositoryAllowlistRecord | None:
        return self._repository_allowlist.get_repository_allowlist_entry(repo_full_name)

    def list_repository_allowlist_entries(self) -> list[RepositoryAllowlistRecord]:
        return self._repository_allowlist.list_repository_allowlist_entries()

    def count_repository_allowlist_entries(self) -> int:
        return self._repository_allowlist.count_repository_allowlist_entries()

    def list_repository_allowlist_events(self, limit: int = 200) -> list[RepositoryAllowlistEventRecord]:
        return self._repository_allowlist.list_repository_allowlist_events(limit)

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh main-database connection under the Store's shared policy."""

        return connect_sqlite(self.database_path, self.sqlite_policy)

    def _ensure_schema(self) -> None:
        """Create or migrate the main schema before serving work."""

        with self._connect() as connection:
            ensure_main_schema(connection)


def _now(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")
