from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.analysis_event_store import SQLiteAnalysisEventStore
from refactor_agent.analysis_events import AnalysisEvent, PublishReceipt
from refactor_agent.artifacts import sanitize_text
from refactor_agent.errors import ErrorCode, public_error_message
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


class JobTransitionError(ValueError):
    pass


_LEGAL_JOB_TRANSITIONS: dict[GitHubJobStatus, set[GitHubJobStatus]] = {
    GitHubJobStatus.QUEUED: {GitHubJobStatus.RUNNING, GitHubJobStatus.CANCELLED},
    GitHubJobStatus.RUNNING: {
        GitHubJobStatus.CANCEL_REQUESTED,
        GitHubJobStatus.TIMED_OUT,
        GitHubJobStatus.SUCCESS,
        GitHubJobStatus.FAILED,
        GitHubJobStatus.DRY_RUN,
    },
    GitHubJobStatus.CANCEL_REQUESTED: {
        GitHubJobStatus.CANCELLED,
        GitHubJobStatus.TIMED_OUT,
        GitHubJobStatus.FAILED,
    },
    GitHubJobStatus.FAILED: {GitHubJobStatus.QUEUED},
    GitHubJobStatus.CANCELLED: {GitHubJobStatus.QUEUED},
    GitHubJobStatus.TIMED_OUT: {GitHubJobStatus.QUEUED},
    GitHubJobStatus.SUCCESS: set(),
    GitHubJobStatus.DRY_RUN: set(),
}


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
        """Insert a queued job once and resolve duplicate delivery/active-job races by reading."""

        now = _now()
        record = GitHubJobRecord(
            job_id=job.job_id,
            job_kind=job.job_kind,
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
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._insert_github_job(connection, record)
                self._insert_job_event(
                    connection,
                    job_id=record.job_id,
                    event_type="JOB_CREATED",
                    from_status=None,
                    to_status=GitHubJobStatus.QUEUED,
                    attempt=0,
                    message="job queued",
                )
                self._analysis_events.insert_task_event(
                    connection,
                    job_id=record.job_id,
                    status=GitHubJobStatus.QUEUED,
                    attempt=0,
                )
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
                WHERE repo_full_name = ? AND issue_number = ?
                  AND status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
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
        deadline_seconds: int = 900,
    ) -> GitHubJobRecord | None:
        """Recover expired leases and claim at most one queued job in one short write transaction."""

        now = _now()
        lease_expires = _now(timedelta(seconds=lease_seconds))
        deadline_at = _now(timedelta(seconds=deadline_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """
                SELECT * FROM github_jobs
                WHERE status IN ('RUNNING', 'CANCEL_REQUESTED') AND lease_expires_at < ?
                """,
                (now,),
            ).fetchall()
            for expired_row in expired:
                source_status = GitHubJobStatus(expired_row["status"])
                if source_status == GitHubJobStatus.CANCEL_REQUESTED:
                    next_status = GitHubJobStatus.CANCELLED
                    error = None
                else:
                    exhausted = expired_row["attempt_count"] >= max_attempts
                    next_status = GitHubJobStatus.FAILED if exhausted else GitHubJobStatus.QUEUED
                    error = "worker lease expired after retry limit" if exhausted else None
                cursor = connection.execute(
                    """
                    UPDATE github_jobs
                    SET status = ?, error = ?, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = ?
                      AND lease_owner IS ? AND lease_expires_at = ?
                    """,
                    (
                        next_status.value,
                        error,
                        now,
                        expired_row["job_id"],
                        source_status.value,
                        expired_row["lease_owner"],
                        expired_row["lease_expires_at"],
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                self._insert_job_event(
                    connection,
                    job_id=expired_row["job_id"],
                    event_type="LEASE_EXPIRED",
                    from_status=source_status,
                    to_status=next_status,
                    worker_id=expired_row["lease_owner"],
                    attempt=expired_row["attempt_count"],
                    message=error or (
                        "expired cancellation lease finalized job"
                        if next_status == GitHubJobStatus.CANCELLED
                        else "expired lease returned job to queue"
                    ),
                )
                self._analysis_events.insert_task_event(
                    connection,
                    job_id=expired_row["job_id"],
                    status=next_status,
                    attempt=expired_row["attempt_count"],
                    run_id=expired_row["run_id"],
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
            cursor = connection.execute(
                """
                UPDATE github_jobs
                SET status = 'RUNNING', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?, deadline_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'QUEUED'
                """,
                (worker_id, lease_expires, deadline_at, now, row["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_job_event(
                connection,
                job_id=row["job_id"],
                event_type="STATE_TRANSITION",
                from_status=GitHubJobStatus.QUEUED,
                to_status=GitHubJobStatus.RUNNING,
                worker_id=worker_id,
                attempt=row["attempt_count"] + 1,
                message="worker claimed job",
            )
            self._analysis_events.insert_task_event(
                connection,
                job_id=row["job_id"],
                status=GitHubJobStatus.RUNNING,
                attempt=row["attempt_count"] + 1,
                run_id=row["run_id"],
                deadline_at=deadline_at,
            )
            claimed = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
        return _job_record_from_row(claimed) if claimed else None

    def mark_github_job_running(self, job_id: str) -> None:
        self.transition_github_job(job_id, GitHubJobStatus.RUNNING)

    def transition_github_job(
        self,
        job_id: str,
        to_status: GitHubJobStatus | str,
        *,
        worker_id: str | None = None,
        message: str = "",
        require_owner: bool = False,
    ) -> GitHubJobRecord:
        """Apply a legal state transition only if the observed status and lease owner still match."""

        destination = GitHubJobStatus(to_status)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobTransitionError(f"job not found: {job_id}")
            source = GitHubJobStatus(row["status"])
            if destination not in _LEGAL_JOB_TRANSITIONS[source]:
                raise JobTransitionError(f"illegal job transition: {source.value} -> {destination.value}")
            terminal_owner_required = (
                row["lease_owner"] is not None
                and destination not in {GitHubJobStatus.RUNNING, GitHubJobStatus.CANCEL_REQUESTED}
            )
            if (require_owner or terminal_owner_required) and (
                not worker_id or row["lease_owner"] != worker_id
            ):
                raise JobTransitionError(f"lease owner mismatch for job {job_id}")
            lease_owner = worker_id if destination == GitHubJobStatus.RUNNING and worker_id else row["lease_owner"]
            lease_expires_at = row["lease_expires_at"]
            if destination not in {GitHubJobStatus.RUNNING, GitHubJobStatus.CANCEL_REQUESTED}:
                lease_owner = None
                lease_expires_at = None
            cursor = connection.execute(
                """
                UPDATE github_jobs
                SET status = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = ? AND lease_owner IS ?
                """,
                (
                    destination.value,
                    lease_owner,
                    lease_expires_at,
                    _now(),
                    job_id,
                    source.value,
                    row["lease_owner"],
                ),
            )
            if cursor.rowcount != 1:
                raise JobTransitionError(f"concurrent job transition rejected: {job_id}")
            self._insert_job_event(
                connection,
                job_id=job_id,
                event_type="STATE_TRANSITION",
                from_status=source,
                to_status=destination,
                worker_id=worker_id,
                attempt=row["attempt_count"],
                message=message,
            )
            self._analysis_events.insert_task_event(
                connection,
                job_id=job_id,
                status=destination,
                attempt=row["attempt_count"],
                run_id=row["run_id"],
                deadline_at=row["deadline_at"],
            )
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated)

    def request_github_job_cancellation(self, job_id: str) -> tuple[GitHubJobRecord, bool]:
        """Cancel or request cancellation with CAS so a heartbeat cannot erase the request."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobTransitionError(f"job not found: {job_id}")
            source = GitHubJobStatus(row["status"])
            if source == GitHubJobStatus.CANCEL_REQUESTED:
                return _job_record_from_row(row), False
            if source == GitHubJobStatus.QUEUED:
                destination = GitHubJobStatus.CANCELLED
            elif source == GitHubJobStatus.RUNNING:
                destination = GitHubJobStatus.CANCEL_REQUESTED
            else:
                raise JobTransitionError(f"cannot cancel terminal job in status {source.value}")
            cursor = connection.execute(
                """
                UPDATE github_jobs
                SET status = ?, lease_owner = CASE WHEN ? = 'CANCELLED' THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? = 'CANCELLED' THEN NULL ELSE lease_expires_at END,
                    updated_at = ?
                WHERE job_id = ? AND status = ? AND lease_owner IS ?
                """,
                (
                    destination.value,
                    destination.value,
                    destination.value,
                    _now(),
                    job_id,
                    source.value,
                    row["lease_owner"],
                ),
            )
            if cursor.rowcount != 1:
                raise JobTransitionError(f"concurrent cancellation rejected: {job_id}")
            self._insert_job_event(
                connection,
                job_id=job_id,
                event_type="CANCEL_REQUESTED",
                from_status=source,
                to_status=destination,
                worker_id=row["lease_owner"],
                attempt=row["attempt_count"],
                message="manual cancellation requested",
            )
            self._analysis_events.insert_task_event(
                connection,
                job_id=job_id,
                status=destination,
                attempt=row["attempt_count"],
                run_id=row["run_id"],
                deadline_at=row["deadline_at"],
            )
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated), True

    def retry_github_job(self, job_id: str) -> GitHubJobRecord:
        """Requeue an eligible terminal job only while its observed state remains unchanged."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobTransitionError(f"job not found: {job_id}")
            source = GitHubJobStatus(row["status"])
            if row["pr_url"]:
                raise JobTransitionError("job with a pull request cannot be retried")
            if source not in {
                GitHubJobStatus.FAILED,
                GitHubJobStatus.CANCELLED,
                GitHubJobStatus.TIMED_OUT,
            }:
                raise JobTransitionError(f"job in status {source.value} cannot be retried")
            try:
                cursor = connection.execute(
                    """
                    UPDATE github_jobs
                    SET status = 'QUEUED', attempt_count = 0, lease_owner = NULL,
                        lease_expires_at = NULL, deadline_at = NULL, error = NULL, updated_at = ?
                    WHERE job_id = ? AND status = ? AND pr_url IS NULL
                    """,
                    (_now(), job_id, source.value),
                )
            except sqlite3.IntegrityError as exc:
                raise JobTransitionError("another active job already exists for this issue") from exc
            if cursor.rowcount != 1:
                raise JobTransitionError(f"concurrent retry rejected: {job_id}")
            self._insert_job_event(
                connection,
                job_id=job_id,
                event_type="MANUAL_RETRY",
                from_status=source,
                to_status=GitHubJobStatus.QUEUED,
                attempt=0,
                message="manual retry queued",
            )
            self._analysis_events.insert_task_event(
                connection,
                job_id=job_id,
                status=GitHubJobStatus.QUEUED,
                attempt=0,
                run_id=row["run_id"],
            )
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated)

    def list_job_events(self, job_id: str, limit: int = 200) -> list[JobEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        return [JobEventRecord(**dict(row)) for row in rows]

    def renew_github_job_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        """Extend a lease only for the current owner of a still-running job."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE github_jobs SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (_now(timedelta(seconds=lease_seconds)), _now(), job_id, worker_id),
            )
        return cursor.rowcount == 1

    def mark_github_job_failed(
        self,
        job_id: str,
        error: str,
        worker_id: str | None = None,
    ) -> None:
        """Record a generic failure without persisting the caller's raw exception text."""

        self._finish_github_job(
            job_id,
            GitHubJobStatus.FAILED,
            worker_id=worker_id,
            error_code=ErrorCode.INTERNAL_ERROR,
            error_message=public_error_message(ErrorCode.INTERNAL_ERROR),
            error_summary="worker job failed",
        )

    def mark_github_job_timed_out(self, job_id: str, error: str, worker_id: str) -> None:
        """Finish an owned job as timed out through the same conditional completion path."""

        self._finish_github_job(
            job_id,
            GitHubJobStatus.TIMED_OUT,
            worker_id=worker_id,
            error=error,
        )

    def complete_github_job(
        self,
        job: GitHubRefactorJob,
        result: GitHubAutomationResult,
        worker_id: str | None = None,
    ) -> GitHubJobRecord:
        """Persist a processor result only if the caller still owns the current lease."""

        return self._finish_github_job(
            job.job_id,
            GitHubJobStatus(result.status),
            worker_id=worker_id,
            branch_name=result.branch_name,
            run_id=result.run_id,
            pr_url=result.pr_url,
            workspace_path=result.workspace_path,
            error=result.error,
            error_code=result.error_code,
            error_message=result.error_message,
            error_summary=result.error_summary,
        )

    def fail_github_job(
        self,
        job: GitHubRefactorJob,
        error: str,
        worker_id: str | None = None,
    ) -> GitHubJobRecord:
        """Adapt a processor exception to the conditional terminal-state update."""

        result = GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            status="FAILED",
            error=error,
        )
        return self.complete_github_job(job, result, worker_id=worker_id)

    def _finish_github_job(
        self,
        job_id: str,
        destination: GitHubJobStatus,
        *,
        worker_id: str | None = None,
        branch_name: str | None = None,
        run_id: str | None = None,
        pr_url: str | None = None,
        workspace_path: Path | None = None,
        error: str | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
        error_summary: str | None = None,
    ) -> GitHubJobRecord:
        """Commit terminal fields and audit events with expected-status/owner compare-and-set."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobTransitionError(f"job not found: {job_id}")
            source = GitHubJobStatus(row["status"])
            if destination not in _LEGAL_JOB_TRANSITIONS[source]:
                raise JobTransitionError(f"illegal job transition: {source.value} -> {destination.value}")
            if row["lease_owner"] is not None and row["lease_owner"] != worker_id:
                raise JobTransitionError(f"lease owner mismatch for job {job_id}")
            cursor = connection.execute(
                """
                UPDATE github_jobs
                SET status = ?, branch_name = COALESCE(?, branch_name),
                    run_id = COALESCE(?, run_id), pr_url = COALESCE(?, pr_url),
                    workspace_path = ?, error = NULL, error_code = ?, error_message = ?, error_summary = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = ? AND lease_owner IS ?
                """,
                (
                    destination.value,
                    branch_name,
                    run_id,
                    pr_url,
                    str(workspace_path) if workspace_path else None,
                    error_code.value if error_code else None,
                    error_message,
                    sanitize_text(error_summary) if error_summary else None,
                    _now(),
                    job_id,
                    source.value,
                    row["lease_owner"],
                ),
            )
            if cursor.rowcount != 1:
                raise JobTransitionError(f"concurrent completion rejected: {job_id}")
            self._insert_job_event(
                connection,
                job_id=job_id,
                event_type="STATE_TRANSITION",
                from_status=source,
                to_status=destination,
                worker_id=worker_id,
                attempt=row["attempt_count"],
                message=error_message or f"job completed with status {destination.value}",
            )
            self._analysis_events.insert_task_event(
                connection,
                job_id=job_id,
                status=destination,
                attempt=row["attempt_count"],
                run_id=run_id or row["run_id"],
                deadline_at=row["deadline_at"],
            )
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated)

    def _insert_github_job(self, connection: sqlite3.Connection, record: GitHubJobRecord) -> None:
        """Insert a new job without any conflict clause that could overwrite a live lease."""

        connection.execute(
                """
                INSERT INTO github_jobs (
                    job_id, job_kind, delivery_id, repo_full_name, issue_number, target_path, tests_path, status,
                    branch_name, run_id, pr_url, workspace_path, error, payload_json, attempt_count,
                    lease_owner, lease_expires_at, created_at, updated_at
                    , deadline_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.job_id,
                    record.job_kind.value,
                    record.delivery_id,
                    record.repo_full_name,
                    record.issue_number,
                    record.target_path,
                    record.tests_path,
                    record.status.value,
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
                    record.deadline_at,
                ),
            )

    def _insert_job_event(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: str,
        from_status: GitHubJobStatus | None,
        to_status: GitHubJobStatus | None,
        attempt: int,
        message: str,
        worker_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (
                event_id, job_id, event_type, from_status, to_status,
                worker_id, attempt, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"event-{uuid4().hex}",
                job_id,
                event_type,
                from_status.value if from_status else None,
                to_status.value if to_status else None,
                worker_id,
                attempt,
                sanitize_text(message)[:2048],
                _now(),
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


def _job_record_from_row(row: sqlite3.Row) -> GitHubJobRecord:
    data = dict(row)
    if data.get("workspace_path"):
        data["workspace_path"] = Path(data["workspace_path"])
    return GitHubJobRecord(**data)
