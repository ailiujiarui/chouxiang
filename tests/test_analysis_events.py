from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType
from refactor_agent.config import AppSettings
from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob
from refactor_agent.store import SQLiteRunStore
from refactor_agent.webhook import create_app


def test_analysis_event_store_is_ordered_idempotent_and_cursor_readable(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "events.sqlite")
    event = AnalysisEvent(
        event_id="event-1",
        event_type=AnalysisEventType.PYTEST_PASSED,
        task_id="task-1",
        run_id="run-1",
        source="orchestrator",
        phase="pytest",
        safe_metrics={"duration_seconds": 1.25, "returncode": 0},
    )

    first = store.emit(event)
    duplicate = store.emit(event)

    assert first.accepted is True
    assert first.sequence == 1
    assert duplicate.duplicate is True
    assert duplicate.sequence == first.sequence
    assert store.latest_analysis_event_sequence() == 1
    assert store.list_analysis_events(after=0) == [event.model_copy(update={"sequence": 1})]
    assert store.list_analysis_events(after=1) == []


def test_job_lifecycle_writes_sanitized_analysis_events_in_transition_transactions(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "events.sqlite")
    job = _job()
    store.create_github_job(job)
    claimed = store.claim_next_github_job("worker-1", lease_seconds=60, max_attempts=3, deadline_seconds=900)
    assert claimed is not None
    store.complete_github_job(
        job,
        GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=None,
            run_id="run-1",
            status="DRY_RUN",
        ),
        worker_id="worker-1",
    )

    events = store.list_analysis_events()

    assert [event.event_type for event in events] == [
        AnalysisEventType.TASK_QUEUED,
        AnalysisEventType.TASK_STARTED,
        AnalysisEventType.TASK_COMPLETED,
    ]
    assert events[1].deadline_at is not None
    assert events[-1].run_id == "run-1"
    assert all("error" not in event.safe_metrics for event in events)


def test_analysis_event_api_lists_events_and_exposes_replay_cursor(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "events.sqlite")
    store.emit(
        AnalysisEvent(
            event_type=AnalysisEventType.TASK_STARTED,
            task_id="task-1",
            source="worker",
        )
    )
    app = create_app(
        AppSettings(
            database_path=store.database_path,
            run_root=tmp_path / "runs",
            allowed_repositories={"octo/demo"},
            sandbox_backend="docker",
            mock_llm=True,
        ),
        store=store,
        start_worker=False,
    )

    with TestClient(app) as client:
        cursor = client.get("/analysis/events/cursor")
        response = client.get("/analysis/events", params={"after": 0})
        invalid = client.get("/analysis/events", params={"after": -1})

    assert cursor.json() == {"latest_sequence": 1}
    assert response.status_code == 200
    assert response.json()["events"][0]["event_type"] == "TASK_STARTED"
    assert response.json()["next_sequence"] == 1
    assert response.json()["latest_sequence"] == 1
    assert response.json()["has_more"] is False
    assert invalid.status_code == 400
    assert "/analysis/events/stream" in app.openapi()["paths"]


def test_analysis_event_retention_prunes_only_old_rows(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "events.sqlite")
    old = datetime.now(timezone.utc) - timedelta(days=31)
    store.emit(
        AnalysisEvent(
            event_type=AnalysisEventType.TASK_FAILED,
            task_id="old-task",
            source="worker",
            occurred_at=old,
        )
    )
    store.emit(
        AnalysisEvent(
            event_type=AnalysisEventType.TASK_COMPLETED,
            task_id="new-task",
            source="worker",
        )
    )

    deleted = store.prune_analysis_events(older_than=datetime.now(timezone.utc) - timedelta(days=30))

    assert deleted == 1
    assert [event.task_id for event in store.list_analysis_events()] == ["new-task"]


def test_public_event_page_skips_private_rows_without_skipping_public_cursor(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "events.sqlite")
    for task_id in ("private-1", "private-2"):
        store.emit(
            AnalysisEvent(
                event_type=AnalysisEventType.TASK_STARTED,
                task_id=task_id,
                source="worker",
                sensitivity="private",
            )
        )
    store.emit(
        AnalysisEvent(
            event_type=AnalysisEventType.TASK_STARTED,
            task_id="public-3",
            source="worker",
        )
    )

    events, next_sequence, latest_sequence, has_more = store.read_public_analysis_event_page(
        after=0,
        limit=2,
    )

    assert [event.task_id for event in events] == ["public-3"]
    assert next_sequence == 3
    assert latest_sequence == 3
    assert has_more is False


def test_safe_metrics_reject_unknown_keys_and_free_form_strings() -> None:
    with pytest.raises(ValidationError):
        AnalysisEvent(
            event_type=AnalysisEventType.PYTEST_FAILED,
            task_id="task-1",
            source="orchestrator",
            safe_metrics={"error": "raw pytest output"},
        )

    with pytest.raises(ValidationError):
        AnalysisEvent(
            event_type=AnalysisEventType.PYTEST_FAILED,
            task_id="task-1",
            source="orchestrator",
            safe_metrics={"job_status": "secret text"},
        )


def _job() -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_id="job-events",
        delivery_id="delivery-events",
        repo_full_name="local/snippet",
        issue_number=None,
        issue_title="Analyze snippet",
        issue_text="simplify",
        target_path="snippet.py",
        tests_path="test_snippet.py",
        event_name="dashboard",
        action="submitted",
    )
