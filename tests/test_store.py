from pathlib import Path
import sqlite3

import pytest

from refactor_agent.errors import ErrorCode, public_error_message
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
            ("legacy-1", None, "repo", 10, 8, 4, 2, 0, "FAILED", "Traceback private-token"),
        )

    record = SQLiteRunStore(database).get("legacy-1")

    assert record is not None
    assert record.evidence_level.value == "REPOSITORY_TESTS"
    assert record.error_code == ErrorCode.INTERNAL_ERROR
    assert record.error_message == public_error_message(ErrorCode.INTERNAL_ERROR)
    assert record.error_summary is None





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








def test_store_rejects_illegal_transition_without_appending_event(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = _github_job()
    store.create_github_job(job)

    with pytest.raises(JobTransitionError, match="QUEUED -> SUCCESS"):
        store.transition_github_job(job.job_id, "SUCCESS")

    assert store.get_github_job(job.job_id).status == "QUEUED"
    assert len(store.list_job_events(job.job_id)) == 1




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
