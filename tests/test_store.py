from pathlib import Path
import sqlite3

import pytest

from refactor_agent.models import (
    BenchmarkCaseRecord,
    BenchmarkRunRecord,
    GitHubAutomationResult,
    GitHubRefactorJob,
    RepositoryJobKind,
    RunRecord,
    TrajectoryMemoryRecord,
)
from refactor_agent.store import JobTransitionError, SQLiteRunStore


def test_store_round_trip(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    record = RunRecord(
        run_id="run-1",
        repo_name="repo",
        pre_loc=10,
        post_loc=2,
        pre_cc=4,
        post_cc=1,
        self_heal_count=1,
        status="SUCCESS",
    )
    store.save(record)
    loaded = store.get("run-1")
    assert loaded == record
    assert store.list_runs()[0] == record


def test_store_migrates_legacy_run_metadata_with_safe_defaults(tmp_path: Path):
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, issue_id TEXT, repo_name TEXT NOT NULL,
                pre_loc INTEGER, post_loc INTEGER, pre_cc INTEGER, post_cc INTEGER,
                self_heal_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
                error TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-1", None, "repo", 10, 8, 4, 2, 0, "SUCCESS", None),
        )

    record = SQLiteRunStore(database).get("legacy-1")

    assert record is not None
    assert record.evidence_level.value == "REPOSITORY_TESTS"
    assert record.report_persona.value == "STRICT"


def test_store_tracks_trajectory_memory(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    record = TrajectoryMemoryRecord(
        memory_id="memory-1",
        run_id="run-1",
        repo_name="repo",
        target_path="leap_year.py",
        status="FAILED",
        lesson="不要再把 1900 当闰年。",
        error_signature="AssertionError",
        reward=None,
    )
    store.save_memory(record)
    store.save_memory(
        TrajectoryMemoryRecord(
            memory_id="memory-2",
            run_id="run-2",
            repo_name="repo",
            target_path="other.py",
            status="SUCCESS",
            lesson="复用集合判断。",
            reward=12.5,
        )
    )
    memories = store.list_memory("repo", "leap_year.py")
    assert len(memories) == 1
    assert memories[0].memory_id == "memory-1"
    assert memories[0].created_at is not None
    assert len(store.list_memory(repo_name="repo")) == 2
    assert len(store.list_memory(target_path="other.py")) == 1
    assert len(store.list_memory()) == 2


def test_store_persists_repository_allowlist_and_append_only_events(tmp_path: Path):
    database = tmp_path / "runs.sqlite"
    first = SQLiteRunStore(database)

    created = first.add_repository_allowlist_entry("octo/demo")
    duplicate = first.add_repository_allowlist_entry("octo/demo")

    assert created is not None
    assert duplicate == created
    assert SQLiteRunStore(database).list_repository_allowlist_entries() == [created]
    assert first.count_repository_allowlist_entries() == 1
    assert first.remove_repository_allowlist_entry("octo/demo") is True
    assert first.remove_repository_allowlist_entry("octo/demo") is False
    assert first.list_repository_allowlist_entries() == []
    assert [event.action for event in first.list_repository_allowlist_events()] == ["ADD", "REMOVE"]


def test_store_tracks_github_job_lifecycle(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_id="job-1",
        delivery_id="delivery-1",
        repo_full_name="octo/demo",
        issue_number=42,
        issue_title="Bug",
        issue_text="target: app.py",
        target_path="app.py",
        tests_path="tests",
        event_name="issues",
        action="opened",
    )
    queued = store.create_github_job(job)
    assert queued.status == "QUEUED"

    store.mark_github_job_running("job-1")
    assert store.get_github_job("job-1").status == "RUNNING"

    completed = store.complete_github_job(
        job,
        GitHubAutomationResult(
            job_id="job-1",
            repo_full_name="octo/demo",
            issue_number=42,
            branch_name="refactor-agent/issue-42",
            run_id="run-1",
            status="SUCCESS",
            pr_url="https://github.com/octo/demo/pull/1",
            workspace_path=tmp_path / "checkout",
        ),
    )
    assert completed.status == "SUCCESS"
    assert completed.pr_url == "https://github.com/octo/demo/pull/1"
    assert store.list_github_jobs()[0].job_id == "job-1"


def test_store_round_trips_dashboard_url_job_without_issue_number(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_kind=RepositoryJobKind.DASHBOARD_URL,
        job_id="url-job-1",
        delivery_id="dashboard:delivery-1",
        repo_full_name="octo/demo",
        default_branch=None,
        issue_number=None,
        issue_title="Dashboard URL 本地简化任务",
        issue_text="简化 calculate 函数",
        target_path="__AUTO__",
        tests_path="tests",
        event_name="dashboard_url",
        action="submitted",
    )

    queued = store.create_github_job(job)
    loaded = store.get_github_job(job.job_id)

    assert queued.job_kind == RepositoryJobKind.DASHBOARD_URL
    assert queued.issue_number is None
    assert loaded is not None
    assert loaded.job_kind == RepositoryJobKind.DASHBOARD_URL
    assert loaded.issue_number is None


def test_store_deduplicates_delivery_and_recovers_expired_lease(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_id="job-1",
        delivery_id="delivery-1",
        repo_full_name="octo/demo",
        issue_number=42,
        issue_title="Bug",
        issue_text="target: app.py",
        target_path="app.py",
        tests_path="tests",
        event_name="issues",
        action="opened",
    )
    assert store.create_github_job(job).job_id == "job-1"
    duplicate = job.model_copy(update={"job_id": "job-duplicate"})
    assert store.create_github_job(duplicate).job_id == "job-1"
    concurrent = job.model_copy(update={"job_id": "job-concurrent", "delivery_id": "delivery-2"})
    assert store.create_github_job(concurrent).job_id == "job-1"

    claimed = store.claim_next_github_job("worker-1", lease_seconds=30, max_attempts=3)
    assert claimed is not None
    assert claimed.status == "RUNNING"
    assert claimed.attempt_count == 1
    with store._connect() as connection:
        connection.execute(
            "UPDATE github_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = 'job-1'"
        )
    reclaimed = store.claim_next_github_job("worker-2", lease_seconds=30, max_attempts=3)
    assert reclaimed is not None
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_owner == "worker-2"


def test_store_finalizes_cancel_request_when_worker_lease_expires(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)
    claimed = store.claim_next_github_job("worker-1", lease_seconds=30, max_attempts=3)
    assert claimed is not None
    store.request_github_job_cancellation(job.job_id)
    with store._connect() as connection:
        connection.execute(
            "UPDATE github_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job.job_id,),
        )

    assert store.claim_next_github_job("worker-2", lease_seconds=30, max_attempts=3) is None

    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "CANCELLED"
    assert store.list_job_events(job.job_id)[-1].to_status == "CANCELLED"


def test_claim_persists_job_deadline_for_dashboard_and_recovery(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    store.create_github_job(_github_job())

    claimed = store.claim_next_github_job(
        "worker-1",
        lease_seconds=30,
        max_attempts=3,
        deadline_seconds=900,
    )

    assert claimed is not None
    assert claimed.deadline_at is not None
    assert claimed.deadline_at > claimed.updated_at


def test_store_records_state_transitions_in_append_only_order(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()

    store.create_github_job(job)
    store.transition_github_job(job.job_id, "RUNNING", worker_id="worker-1")
    store.transition_github_job(
        job.job_id,
        "SUCCESS",
        worker_id="worker-1",
        message="pull request created",
        require_owner=True,
    )

    events = store.list_job_events(job.job_id)
    assert [(event.from_status, event.to_status) for event in events] == [
        (None, "QUEUED"),
        ("QUEUED", "RUNNING"),
        ("RUNNING", "SUCCESS"),
    ]
    assert events[-1].message == "pull request created"
    assert events[-1].worker_id == "worker-1"


def test_store_rejects_illegal_transition_without_appending_event(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)

    with pytest.raises(JobTransitionError, match="QUEUED -> SUCCESS"):
        store.transition_github_job(job.job_id, "SUCCESS")

    assert store.get_github_job(job.job_id).status == "QUEUED"
    assert len(store.list_job_events(job.job_id)) == 1


def test_store_sanitizes_persisted_event_messages(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)

    store.transition_github_job(
        job.job_id,
        "CANCELLED",
        message="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    )

    message = store.list_job_events(job.job_id)[-1].message
    assert "abcdefghijklmnopqrstuvwxyz" not in message
    assert "[REDACTED]" in message


def test_store_rejects_terminal_transition_from_stale_lease_owner(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)
    claimed = store.claim_next_github_job("worker-current", lease_seconds=30, max_attempts=3)
    assert claimed is not None

    with pytest.raises(JobTransitionError, match="lease owner"):
        store.transition_github_job(
            job.job_id,
            "FAILED",
            worker_id="worker-stale",
            message="late failure",
            require_owner=True,
        )

    assert store.get_github_job(job.job_id).status == "RUNNING"
    assert [event.to_status for event in store.list_job_events(job.job_id)] == ["QUEUED", "RUNNING"]


def test_store_requires_owner_when_completing_a_leased_job(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)
    assert store.claim_next_github_job("worker-current", lease_seconds=30, max_attempts=3) is not None

    with pytest.raises(JobTransitionError, match="lease owner"):
        store.complete_github_job(
            job,
            GitHubAutomationResult(
                job_id=job.job_id,
                repo_full_name=job.repo_full_name,
                issue_number=job.issue_number,
                status="SUCCESS",
            ),
        )

    assert store.get_github_job(job.job_id).status == "RUNNING"


def test_store_supports_cancelled_and_timed_out_terminal_states(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    first = _github_job()
    store.create_github_job(first)
    store.transition_github_job(first.job_id, "CANCELLED", message="cancelled before claim")

    second = _github_job(job_id="job-2", delivery_id="delivery-2", issue_number=43)
    store.create_github_job(second)
    store.transition_github_job(second.job_id, "RUNNING", worker_id="worker-1")
    store.transition_github_job(
        second.job_id,
        "TIMED_OUT",
        worker_id="worker-1",
        require_owner=True,
    )

    assert store.get_github_job(first.job_id).status == "CANCELLED"
    assert store.get_github_job(second.job_id).status == "TIMED_OUT"


def test_store_requests_cancel_for_queued_and_running_jobs(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    queued = _github_job()
    store.create_github_job(queued)

    cancelled, accepted = store.request_github_job_cancellation(queued.job_id)
    assert accepted is True
    assert cancelled.status == "CANCELLED"

    running = _github_job(job_id="job-running", delivery_id="delivery-running", issue_number=43)
    store.create_github_job(running)
    store.claim_next_github_job("worker-1", lease_seconds=30, max_attempts=3)
    requested, accepted = store.request_github_job_cancellation(running.job_id)
    repeated, repeated_accepted = store.request_github_job_cancellation(running.job_id)

    assert accepted is True
    assert requested.status == "CANCEL_REQUESTED"
    assert repeated.status == "CANCEL_REQUESTED"
    assert repeated_accepted is False


def test_store_manual_retry_resets_attempt_and_preserves_events(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)
    claimed = store.claim_next_github_job("worker-1", lease_seconds=30, max_attempts=3)
    assert claimed is not None
    store.transition_github_job(
        job.job_id,
        "FAILED",
        worker_id="worker-1",
        require_owner=True,
        message="provider failed",
    )
    event_count = len(store.list_job_events(job.job_id))

    retried = store.retry_github_job(job.job_id)

    assert retried.status == "QUEUED"
    assert retried.attempt_count == 0
    assert retried.lease_owner is None
    assert len(store.list_job_events(job.job_id)) == event_count + 1


def test_store_rejects_manual_retry_when_pull_request_exists(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)
    with store._connect() as connection:
        connection.execute(
            "UPDATE github_jobs SET status = 'FAILED', pr_url = ? WHERE job_id = ?",
            ("https://github.com/octo/demo/pull/1", job.job_id),
        )

    with pytest.raises(JobTransitionError, match="pull request"):
        store.retry_github_job(job.job_id)


def test_store_round_trips_benchmark_run_and_case_evidence(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    run = BenchmarkRunRecord(
        run_id="benchmark-1",
        manifest_hash="a" * 64,
        provider="mock",
        model="deterministic-gold",
        status="SUCCESS",
        generated_at="2026-07-14T00:00:00+00:00",
    )
    case = BenchmarkCaseRecord(
        run_id=run.run_id,
        case_name="more-take-off-by-one",
        repository="more-itertools/more-itertools",
        commit="da37f9de442b69fbcaa9f54fb042c2a6999473a6",
        provider="mock",
        model="deterministic-gold",
        status="SUCCESS",
        expected_status="SUCCESS",
        attempts=1,
        runtime_seconds=1.5,
        total_tokens=0,
        cost_usd=0,
        normalized_hash="b" * 64,
    )

    store.save_benchmark_run(run, [case])

    assert store.get_benchmark_run(run.run_id) == run
    assert store.list_benchmark_case_results(run.run_id) == [case]


def test_store_migrates_old_job_table_before_creating_event_foreign_key(tmp_path: Path):
    database = tmp_path / "old.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE github_jobs (
                job_id TEXT PRIMARY KEY,
                repo_full_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                target_path TEXT NOT NULL,
                tests_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'DRY_RUN')),
                branch_name TEXT, run_id TEXT, pr_url TEXT, workspace_path TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )

    SQLiteRunStore(database)

    with sqlite3.connect(database) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_events'"
        ).fetchone()[0]
    assert "REFERENCES github_jobs(job_id)" in sql
    assert "github_jobs_legacy" not in sql


def test_store_migrates_existing_control_table_for_dashboard_url_jobs(tmp_path: Path):
    database = tmp_path / "control.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE github_jobs (
                job_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL UNIQUE,
                repo_full_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                target_path TEXT NOT NULL,
                tests_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'CANCELLED',
                    'TIMED_OUT', 'SUCCESS', 'FAILED', 'DRY_RUN'
                )),
                branch_name TEXT, run_id TEXT, pr_url TEXT, workspace_path TEXT,
                error TEXT, payload_json TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT, lease_expires_at TEXT, deadline_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE job_events (
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
            INSERT INTO github_jobs (
                job_id, delivery_id, repo_full_name, issue_number, target_path,
                tests_path, status, created_at, updated_at
            ) VALUES ('old-job', 'old-delivery', 'octo/demo', 42, 'app.py', 'tests',
                      'SUCCESS', '2026-07-15T00:00:00+00:00', '2026-07-15T00:00:00+00:00')
            """
        )

    store = SQLiteRunStore(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(github_jobs)")}
        event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_events'"
        ).fetchone()[0]
    migrated = store.get_github_job("old-job")
    assert migrated is not None
    assert migrated.job_kind == RepositoryJobKind.GITHUB_WEBHOOK
    assert columns["issue_number"][3] == 0
    assert "job_kind" in columns
    assert "REFERENCES github_jobs(job_id)" in event_sql
    assert "github_jobs_legacy" not in event_sql


def _github_job(
    *,
    job_id: str = "job-state",
    delivery_id: str = "delivery-state",
    issue_number: int = 42,
) -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_id=job_id,
        delivery_id=delivery_id,
        repo_full_name="octo/demo",
        issue_number=issue_number,
        issue_title="Bug",
        issue_text="target: app.py",
        target_path="app.py",
        tests_path="tests",
        event_name="issues",
        action="opened",
    )
