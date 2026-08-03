from __future__ import annotations

import sqlite3

from refactor_agent.errors import ErrorCode, public_error_message
from refactor_agent.store_schema import ensure_main_schema


EXPECTED_TABLES = {
    "analysis_events",
    "benchmark_case_results",
    "benchmark_runs",
    "github_jobs",
    "job_events",
    "repository_allowlist",
    "repository_allowlist_events",
    "runs",
    "trajectory_memory",
}

EXPECTED_INDEXES = {
    "idx_analysis_events_task_sequence",
    "idx_github_jobs_active_issue",
    "idx_github_jobs_delivery",
    "idx_job_events_job",
    "idx_repository_allowlist_events_created",
    "idx_trajectory_memory_lookup",
}


def test_ensure_main_schema_creates_expected_tables_and_indexes_idempotently() -> None:
    connection = _connection()
    try:
        with connection:
            ensure_main_schema(connection)
            ensure_main_schema(connection)

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()

    assert tables == EXPECTED_TABLES
    assert indexes == EXPECTED_INDEXES


def test_ensure_main_schema_migrates_legacy_run_contract() -> None:
    connection = _connection()
    try:
        connection.execute(
            """
            CREATE TABLE runs (
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
            INSERT INTO runs (
                run_id, issue_id, repo_name, self_heal_count, status, error
            ) VALUES ('legacy-run', NULL, 'legacy-repo', 0, 'FAILED', 'private traceback')
            """
        )

        with connection:
            ensure_main_schema(connection)

        row = connection.execute(
            """
            SELECT status, error, error_code, error_message, error_summary,
                   evidence_level, report_persona
            FROM runs WHERE run_id = 'legacy-run'
            """
        ).fetchone()
    finally:
        connection.close()

    assert row is not None
    assert dict(row) == {
        "status": "FAILED",
        "error": None,
        "error_code": ErrorCode.INTERNAL_ERROR.value,
        "error_message": public_error_message(ErrorCode.INTERNAL_ERROR),
        "error_summary": None,
        "evidence_level": "REPOSITORY_TESTS",
        "report_persona": "STRICT",
    }


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
