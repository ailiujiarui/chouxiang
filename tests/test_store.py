from pathlib import Path

from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob, RunRecord, TrajectoryMemoryRecord
from refactor_agent.store import SQLiteRunStore


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


def test_store_tracks_github_job_lifecycle(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_id="job-1",
        repo_full_name="octo/demo",
        clone_url="https://github.com/octo/demo.git",
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
