from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.artifacts import sanitize_text
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
                    sanitize_text(record.error) if record.error else None,
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR REPLACE INTO benchmark_runs (
                    run_id, manifest_hash, provider, model, status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
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
                connection.execute(
                    """
                    UPDATE github_jobs
                    SET status = ?, error = ?, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (next_status.value, error, now, expired_row["job_id"], source_status.value),
                )
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
                    lease_owner = ?, lease_expires_at = ?, deadline_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'QUEUED'
                """,
                (worker_id, lease_expires, deadline_at, now, row["job_id"]),
            )
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
            connection.execute(
                """
                UPDATE github_jobs
                SET status = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (destination.value, lease_owner, lease_expires_at, _now(), job_id),
            )
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
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated)

    def request_github_job_cancellation(self, job_id: str) -> tuple[GitHubJobRecord, bool]:
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
            connection.execute(
                """
                UPDATE github_jobs
                SET status = ?, lease_owner = CASE WHEN ? = 'CANCELLED' THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? = 'CANCELLED' THEN NULL ELSE lease_expires_at END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (destination.value, destination.value, destination.value, _now(), job_id),
            )
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
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated), True

    def retry_github_job(self, job_id: str) -> GitHubJobRecord:
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
                connection.execute(
                    """
                    UPDATE github_jobs
                    SET status = 'QUEUED', attempt_count = 0, lease_owner = NULL,
                        lease_expires_at = NULL, deadline_at = NULL, error = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (_now(), job_id),
                )
            except sqlite3.IntegrityError as exc:
                raise JobTransitionError("another active job already exists for this issue") from exc
            self._insert_job_event(
                connection,
                job_id=job_id,
                event_type="MANUAL_RETRY",
                from_status=source,
                to_status=GitHubJobStatus.QUEUED,
                attempt=0,
                message="manual retry queued",
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
        self._finish_github_job(
            job_id,
            GitHubJobStatus.FAILED,
            worker_id=worker_id,
            error=error,
        )

    def mark_github_job_timed_out(self, job_id: str, error: str, worker_id: str) -> None:
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
        return self._finish_github_job(
            job.job_id,
            GitHubJobStatus(result.status),
            worker_id=worker_id,
            branch_name=result.branch_name,
            run_id=result.run_id,
            pr_url=result.pr_url,
            workspace_path=result.workspace_path,
            error=result.error,
        )

    def fail_github_job(
        self,
        job: GitHubRefactorJob,
        error: str,
        worker_id: str | None = None,
    ) -> GitHubJobRecord:
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
    ) -> GitHubJobRecord:
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
            connection.execute(
                """
                UPDATE github_jobs
                SET status = ?, branch_name = COALESCE(?, branch_name),
                    run_id = COALESCE(?, run_id), pr_url = COALESCE(?, pr_url),
                    workspace_path = ?, error = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    destination.value,
                    branch_name,
                    run_id,
                    pr_url,
                    str(workspace_path) if workspace_path else None,
                    sanitize_text(error) if error else None,
                    _now(),
                    job_id,
                ),
            )
            self._insert_job_event(
                connection,
                job_id=job_id,
                event_type="STATE_TRANSITION",
                from_status=source,
                to_status=destination,
                worker_id=worker_id,
                attempt=row["attempt_count"],
                message=sanitize_text(error) if error else f"job completed with status {destination.value}",
            )
            updated = connection.execute(
                "SELECT * FROM github_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_record_from_row(updated)

    def save_github_job(self, record: GitHubJobRecord) -> None:
        with self._connect() as connection:
            self._insert_github_job(connection, record)

    def _insert_github_job(self, connection: sqlite3.Connection, record: GitHubJobRecord) -> None:
        connection.execute(
                """
                INSERT INTO github_jobs (
                    job_id, job_kind, delivery_id, repo_full_name, issue_number, target_path, tests_path, status,
                    branch_name, run_id, pr_url, workspace_path, error, payload_json, attempt_count,
                    lease_owner, lease_expires_at, created_at, updated_at
                    , deadline_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    delivery_id=excluded.delivery_id,
                    job_kind=excluded.job_kind,
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
                    deadline_at=excluded.deadline_at,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
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
            self._insert_repository_allowlist_event(connection, "ADD", repo_full_name)
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
                self._insert_repository_allowlist_event(connection, "REMOVE", repo_full_name)
        return removed

    def get_repository_allowlist_entry(self, repo_full_name: str) -> RepositoryAllowlistRecord | None:
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

    def list_repository_allowlist_events(self, limit: int = 200) -> list[RepositoryAllowlistEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM repository_allowlist_events
                ORDER BY rowid LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [RepositoryAllowlistEventRecord(**dict(row)) for row in rows]

    def _insert_repository_allowlist_event(
        self,
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
                    job_kind TEXT NOT NULL DEFAULT 'GITHUB_WEBHOOK' CHECK(job_kind IN (
                        'GITHUB_WEBHOOK', 'DASHBOARD_URL'
                    )),
                    delivery_id TEXT NOT NULL UNIQUE,
                    repo_full_name TEXT NOT NULL,
                    issue_number INTEGER,
                    target_path TEXT NOT NULL,
                    tests_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'CANCELLED',
                        'TIMED_OUT', 'SUCCESS', 'FAILED', 'DRY_RUN'
                    )),
                    branch_name TEXT,
                    run_id TEXT,
                    pr_url TEXT,
                    workspace_path TEXT,
                    error TEXT,
                    payload_json TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    deadline_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            _migrate_github_jobs(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    worker_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES github_jobs(job_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    run_id TEXT PRIMARY KEY,
                    manifest_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
                    generated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS benchmark_case_results (
                    run_id TEXT NOT NULL,
                    case_name TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_status TEXT NOT NULL,
                    failure_category TEXT,
                    attempts INTEGER NOT NULL,
                    loc_before INTEGER,
                    loc_after INTEGER,
                    cc_before INTEGER,
                    cc_after INTEGER,
                    mutation_kill_rate REAL,
                    adversarial_passed INTEGER,
                    runtime_seconds REAL NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    normalized_hash TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (run_id, case_name),
                    FOREIGN KEY(run_id) REFERENCES benchmark_runs(run_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events (job_id, created_at)"
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repository_allowlist (
                    repo_full_name TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repository_allowlist_events (
                    event_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL CHECK(action IN ('ADD', 'REMOVE')),
                    repo_full_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_repository_allowlist_events_created
                ON repository_allowlist_events (created_at, event_id)
                """
            )


def _now(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _job_record_from_row(row: sqlite3.Row) -> GitHubJobRecord:
    data = dict(row)
    if data.get("workspace_path"):
        data["workspace_path"] = Path(data["workspace_path"])
    return GitHubJobRecord(**data)


def _migrate_github_jobs(connection: sqlite3.Connection) -> None:
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'github_jobs'"
    ).fetchone()
    table_sql = table_sql_row["sql"] if table_sql_row else ""
    table_info = connection.execute("PRAGMA table_info(github_jobs)").fetchall()
    columns = {row["name"]: row for row in table_info}
    issue_number_required = bool(columns.get("issue_number") and columns["issue_number"]["notnull"])
    if "CANCEL_REQUESTED" not in table_sql or "job_kind" not in columns or issue_number_required:
        _rebuild_github_jobs_with_control_states(connection)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(github_jobs)").fetchall()}
    additions = {
        "delivery_id": "TEXT",
        "payload_json": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "lease_owner": "TEXT",
        "lease_expires_at": "TEXT",
        "deadline_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE github_jobs ADD COLUMN {name} {definition}")
    connection.execute("UPDATE github_jobs SET delivery_id = job_id WHERE delivery_id IS NULL")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_github_jobs_delivery ON github_jobs (delivery_id)"
    )
    connection.execute(
        "DROP INDEX IF EXISTS idx_github_jobs_active_issue"
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX idx_github_jobs_active_issue
        ON github_jobs (repo_full_name, issue_number)
        WHERE issue_number IS NOT NULL
          AND status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
        """
    )


def _rebuild_github_jobs_with_control_states(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_github_jobs_delivery")
    connection.execute("DROP INDEX IF EXISTS idx_github_jobs_active_issue")
    connection.execute("DROP TABLE IF EXISTS github_jobs_new")
    connection.execute(
        """
        CREATE TABLE github_jobs_new (
            job_id TEXT PRIMARY KEY,
            job_kind TEXT NOT NULL DEFAULT 'GITHUB_WEBHOOK' CHECK(job_kind IN (
                'GITHUB_WEBHOOK', 'DASHBOARD_URL'
            )),
            delivery_id TEXT NOT NULL UNIQUE,
            repo_full_name TEXT NOT NULL,
            issue_number INTEGER,
            target_path TEXT NOT NULL,
            tests_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'CANCELLED',
                'TIMED_OUT', 'SUCCESS', 'FAILED', 'DRY_RUN'
            )),
            branch_name TEXT,
            run_id TEXT,
            pr_url TEXT,
            workspace_path TEXT,
            error TEXT,
            payload_json TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            deadline_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    legacy_columns = {row["name"] for row in connection.execute("PRAGMA table_info(github_jobs)")}
    columns = [
        "job_id", "job_kind", "delivery_id", "repo_full_name", "issue_number", "target_path", "tests_path",
        "status", "branch_name", "run_id", "pr_url", "workspace_path", "error", "payload_json",
        "attempt_count", "lease_owner", "lease_expires_at", "deadline_at", "created_at", "updated_at",
    ]
    select_parts = []
    for name in columns:
        if name in legacy_columns:
            select_parts.append(name)
        elif name == "job_kind":
            select_parts.append("'GITHUB_WEBHOOK'")
        elif name == "delivery_id":
            select_parts.append("job_id")
        elif name == "attempt_count":
            select_parts.append("0")
        else:
            select_parts.append("NULL")
    connection.execute(
        f"INSERT INTO github_jobs_new ({', '.join(columns)}) SELECT {', '.join(select_parts)} FROM github_jobs"
    )
    connection.execute("DROP TABLE github_jobs")
    connection.execute("ALTER TABLE github_jobs_new RENAME TO github_jobs")
