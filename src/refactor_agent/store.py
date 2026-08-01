from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from refactor_agent.analysis_event_store import SQLiteAnalysisEventStore
from refactor_agent.analysis_events import AnalysisEvent, PublishReceipt
from refactor_agent.artifacts import sanitize_text
from refactor_agent.errors import ErrorCode, public_error_message
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
        """Upsert only fields owned by a complete run snapshot, avoiding REPLACE semantics."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, issue_id, repo_name, pre_loc, post_loc, pre_cc, post_cc,
                    self_heal_count, status, error, error_code, error_message, error_summary,
                    evidence_level, report_persona
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    issue_id = excluded.issue_id,
                    repo_name = excluded.repo_name,
                    pre_loc = excluded.pre_loc,
                    post_loc = excluded.post_loc,
                    pre_cc = excluded.pre_cc,
                    post_cc = excluded.post_cc,
                    self_heal_count = excluded.self_heal_count,
                    status = excluded.status,
                    error = excluded.error,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    error_summary = excluded.error_summary,
                    evidence_level = excluded.evidence_level,
                    report_persona = excluded.report_persona
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
                    None,
                    record.error_code.value if record.error_code else None,
                    record.error_message,
                    sanitize_text(record.error_summary) if record.error_summary else None,
                    record.evidence_level.value,
                    record.report_persona.value,
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

    def save_benchmark_run(
        self,
        run: BenchmarkRunRecord,
        cases: list[BenchmarkCaseRecord],
    ) -> None:
        """Atomically replace one benchmark's owned header and complete case-result set."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO benchmark_runs (
                    run_id, manifest_hash, provider, model, status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    manifest_hash = excluded.manifest_hash,
                    provider = excluded.provider,
                    model = excluded.model,
                    status = excluded.status,
                    generated_at = excluded.generated_at
                """,
                (
                    run.run_id,
                    run.manifest_hash,
                    run.provider,
                    run.model,
                    run.status,
                    run.generated_at,
                ),
            )
            connection.execute("DELETE FROM benchmark_case_results WHERE run_id = ?", (run.run_id,))
            for case in cases:
                connection.execute(
                    """
                    INSERT INTO benchmark_case_results (
                        run_id, case_name, repository, commit_sha, provider, model,
                        status, expected_status, failure_category, attempts,
                        loc_before, loc_after, cc_before, cc_after,
                        mutation_kill_rate, adversarial_passed, runtime_seconds,
                        prompt_tokens, completion_tokens, total_tokens, cost_usd,
                        normalized_hash, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case.run_id,
                        case.case_name,
                        case.repository,
                        case.commit,
                        case.provider,
                        case.model,
                        case.status,
                        case.expected_status,
                        case.failure_category,
                        case.attempts,
                        case.loc_before,
                        case.loc_after,
                        case.cc_before,
                        case.cc_after,
                        case.mutation_kill_rate,
                        int(case.adversarial_passed) if case.adversarial_passed is not None else None,
                        case.runtime_seconds,
                        case.prompt_tokens,
                        case.completion_tokens,
                        case.total_tokens,
                        case.cost_usd,
                        case.normalized_hash,
                        case.error,
                    ),
                )

    def get_benchmark_run(self, run_id: str) -> BenchmarkRunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM benchmark_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return BenchmarkRunRecord(**dict(row)) if row else None

    def list_benchmark_runs(self, limit: int = 20) -> list[BenchmarkRunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM benchmark_runs ORDER BY generated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [BenchmarkRunRecord(**dict(row)) for row in rows]

    def list_benchmark_case_results(self, run_id: str) -> list[BenchmarkCaseRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, case_name, repository, commit_sha AS "commit",
                       provider, model, status, expected_status, failure_category,
                       attempts, loc_before, loc_after, cc_before, cc_after,
                       mutation_kill_rate, adversarial_passed, runtime_seconds,
                       prompt_tokens, completion_tokens, total_tokens, cost_usd,
                       normalized_hash, error
                FROM benchmark_case_results
                WHERE run_id = ? ORDER BY case_name
                """,
                (run_id,),
            ).fetchall()
        return [BenchmarkCaseRecord(**dict(row)) for row in rows]

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
