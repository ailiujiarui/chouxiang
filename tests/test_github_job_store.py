from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from refactor_agent.analysis_event_store import SQLiteAnalysisEventStore
from refactor_agent.analysis_events import AnalysisEventType
from refactor_agent.github_job_store import JobTransitionError, SQLiteGitHubJobStore
from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob
from refactor_agent.store import JobTransitionError as PublicJobTransitionError
from refactor_agent.store_schema import ensure_main_schema


def test_job_repository_owns_lifecycle_leases_and_audit_events(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "jobs.sqlite")
    _initialize(factory)
    analysis_events = SQLiteAnalysisEventStore(factory)
    repository = SQLiteGitHubJobStore(factory, analysis_events)
    job = _github_job()

    queued = repository.create_github_job(job)
    claimed = repository.claim_next_github_job(
        "worker-1",
        lease_seconds=30,
        max_attempts=3,
        deadline_seconds=900,
    )
    completed = repository.complete_github_job(
        job,
        GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            status="SUCCESS",
            run_id="run-1",
        ),
        worker_id="worker-1",
    )

    assert queued.status.value == "QUEUED"
    assert claimed is not None
    assert claimed.status.value == "RUNNING"
    assert claimed.lease_owner == "worker-1"
    assert claimed.deadline_at is not None
    assert completed.status.value == "SUCCESS"
    assert completed.lease_owner is None
    assert repository.get_github_job_by_delivery(job.delivery_id) == completed
    assert repository.get_active_github_job(job.repo_full_name, job.issue_number) is None
    assert repository.list_github_jobs() == [completed]
    assert [event.to_status for event in repository.list_job_events(job.job_id)] == [
        "QUEUED",
        "RUNNING",
        "SUCCESS",
    ]
    assert [event.event_type for event in analysis_events.list_analysis_events()] == [
        AnalysisEventType.TASK_QUEUED,
        AnalysisEventType.TASK_STARTED,
        AnalysisEventType.TASK_COMPLETED,
    ]
    assert PublicJobTransitionError is JobTransitionError


def test_job_record_audit_and_analysis_projection_rollback_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _ConnectionFactory(tmp_path / "jobs.sqlite")
    _initialize(factory)
    analysis_events = SQLiteAnalysisEventStore(factory)
    repository = SQLiteGitHubJobStore(factory, analysis_events)
    job = _github_job()

    def fail_analysis_projection(
        connection: sqlite3.Connection,
        **values: object,
    ) -> None:
        raise RuntimeError("analysis projection failed")

    monkeypatch.setattr(analysis_events, "insert_task_event", fail_analysis_projection)

    with pytest.raises(RuntimeError, match="analysis projection failed"):
        repository.create_github_job(job)

    assert repository.get_github_job(job.job_id) is None
    assert repository.list_job_events(job.job_id) == []
    assert analysis_events.list_analysis_events() == []


def test_job_repository_closes_every_operation_connection(tmp_path: Path) -> None:
    factory = _TrackingConnectionFactory(tmp_path / "jobs.sqlite")
    _initialize(factory)
    repository = SQLiteGitHubJobStore(factory, SQLiteAnalysisEventStore(factory))
    job = _github_job()

    repository.create_github_job(job)
    assert repository.get_github_job(job.job_id) is not None

    assert factory.connections
    assert all(connection.closed for connection in factory.connections)


class _ConnectionFactory:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def __call__(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class _TrackingConnection(sqlite3.Connection):
    closed = False

    def close(self) -> None:
        super().close()
        self.closed = True


class _TrackingConnectionFactory(_ConnectionFactory):
    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.connections: list[_TrackingConnection] = []

    def __call__(self) -> _TrackingConnection:
        connection = sqlite3.connect(self.database_path, factory=_TrackingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        self.connections.append(connection)
        return connection


def _initialize(factory: _ConnectionFactory) -> None:
    connection = factory()
    try:
        with connection:
            ensure_main_schema(connection)
    finally:
        connection.close()


def _github_job() -> GitHubRefactorJob:
    return GitHubRefactorJob(
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
