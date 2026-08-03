"""Main SQLite schema creation and migration helpers."""

from __future__ import annotations

import sqlite3

from refactor_agent.errors import ErrorCode, public_error_message


def ensure_main_schema(connection: sqlite3.Connection) -> None:
    """Create or migrate the main schema on an already configured connection."""

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
            status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED', 'REVIEWED')),
            error TEXT
            ,error_code TEXT
            ,error_message TEXT
            ,error_summary TEXT
            ,evidence_level TEXT NOT NULL DEFAULT 'REPOSITORY_TESTS'
            ,report_persona TEXT NOT NULL DEFAULT 'STRICT'
        )
        """
    )
    _migrate_runs_status(connection)
    _migrate_runs_metadata(connection)
    _migrate_error_fields(connection, "runs")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS github_jobs (
            job_id TEXT PRIMARY KEY,
            job_kind TEXT NOT NULL DEFAULT 'GITHUB_WEBHOOK' CHECK(job_kind IN (
                'GITHUB_WEBHOOK', 'DASHBOARD_URL', 'SNIPPET'
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
            error_code TEXT,
            error_message TEXT,
            error_summary TEXT,
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
    _migrate_error_fields(connection, "github_jobs")
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
        CREATE TABLE IF NOT EXISTS analysis_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            schema_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            task_id TEXT NOT NULL,
            run_id TEXT,
            source TEXT NOT NULL,
            phase TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            evidence_level TEXT,
            error_category TEXT,
            recoverable INTEGER,
            deadline_at TEXT,
            safe_metrics_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            sensitivity TEXT NOT NULL DEFAULT 'public'
                CHECK(sensitivity IN ('public', 'private', 'blocked'))
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_analysis_events_task_sequence
        ON analysis_events (task_id, sequence)
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


def _migrate_runs_status(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    if row is None or "REVIEWED" in (row["sql"] or ""):
        return
    connection.execute("ALTER TABLE runs RENAME TO runs_legacy")
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
            status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED', 'REVIEWED')),
            error TEXT,
            evidence_level TEXT NOT NULL DEFAULT 'REPOSITORY_TESTS',
            report_persona TEXT NOT NULL DEFAULT 'STRICT'
        )
        """
    )
    legacy_columns = {
        column["name"] for column in connection.execute("PRAGMA table_info(runs_legacy)")
    }
    common_columns = [
        column
        for column in (
            "run_id", "issue_id", "repo_name", "pre_loc", "post_loc", "pre_cc", "post_cc",
            "self_heal_count", "status", "error", "evidence_level", "report_persona",
        )
        if column in legacy_columns
    ]
    column_list = ", ".join(common_columns)
    connection.execute(
        f"INSERT INTO runs ({column_list}) SELECT {column_list} FROM runs_legacy"
    )
    connection.execute("DROP TABLE runs_legacy")


def _migrate_runs_metadata(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
    if "evidence_level" not in columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN evidence_level TEXT NOT NULL DEFAULT 'REPOSITORY_TESTS'"
        )
    if "report_persona" not in columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN report_persona TEXT NOT NULL DEFAULT 'STRICT'"
        )


def _migrate_error_fields(connection: sqlite3.Connection, table: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name in ("error_code", "error_message", "error_summary"):
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
    connection.execute(
        f"""
        UPDATE {table}
        SET error_code = ?, error_message = ?, error_summary = NULL, error = NULL
        WHERE error IS NOT NULL AND error_code IS NULL
        """,
        (ErrorCode.INTERNAL_ERROR.value, public_error_message(ErrorCode.INTERNAL_ERROR)),
    )


def _migrate_github_jobs(connection: sqlite3.Connection) -> None:
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'github_jobs'"
    ).fetchone()
    table_sql = table_sql_row["sql"] if table_sql_row else ""
    table_info = connection.execute("PRAGMA table_info(github_jobs)").fetchall()
    columns = {row["name"]: row for row in table_info}
    issue_number_required = bool(columns.get("issue_number") and columns["issue_number"]["notnull"])
    if (
        "CANCEL_REQUESTED" not in table_sql
        or "SNIPPET" not in table_sql
        or "job_kind" not in columns
        or issue_number_required
    ):
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
    connection.execute("DROP INDEX IF EXISTS idx_github_jobs_active_issue")
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
                'GITHUB_WEBHOOK', 'DASHBOARD_URL', 'SNIPPET'
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
