"""SQLite persistence for run snapshots and benchmark result records."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from refactor_agent.artifacts import sanitize_text
from refactor_agent.models import BenchmarkCaseRecord, BenchmarkRunRecord, RunRecord


ConnectionFactory = Callable[[], sqlite3.Connection]


class SQLiteRunRecordStore:
    """Own completed run snapshots and atomic benchmark result sets."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def save(self, record: RunRecord) -> None:
        """Upsert only fields owned by a complete run snapshot, avoiding REPLACE semantics."""

        with self._connection() as connection:
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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return RunRecord(**dict(row)) if row else None

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        with self._connection() as connection:
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

        with self._connection() as connection:
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
            connection.execute(
                "DELETE FROM benchmark_case_results WHERE run_id = ?",
                (run.run_id,),
            )
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
                        int(case.adversarial_passed)
                        if case.adversarial_passed is not None
                        else None,
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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM benchmark_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return BenchmarkRunRecord(**dict(row)) if row else None

    def list_benchmark_runs(self, limit: int = 20) -> list[BenchmarkRunRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM benchmark_runs ORDER BY generated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [BenchmarkRunRecord(**dict(row)) for row in rows]

    def list_benchmark_case_results(self, run_id: str) -> list[BenchmarkCaseRecord]:
        with self._connection() as connection:
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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back a fresh operation connection, then always close it."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
