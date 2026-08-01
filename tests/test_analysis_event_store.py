from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from refactor_agent.analysis_event_store import SQLiteAnalysisEventStore
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType
from refactor_agent.models import GitHubJobStatus
from refactor_agent.store_schema import ensure_main_schema


def test_analysis_event_repository_round_trips_and_deduplicates_events(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "events.sqlite")
    _initialize(factory)
    repository = SQLiteAnalysisEventStore(factory)
    event = AnalysisEvent(
        event_id="event-1",
        event_type=AnalysisEventType.PYTEST_PASSED,
        task_id="task-1",
        run_id="run-1",
        source="orchestrator",
        phase="pytest",
        safe_metrics={"duration_seconds": 1.25, "returncode": 0},
    )

    first = repository.emit(event)
    duplicate = repository.emit(event)
    events, next_sequence, latest_sequence, has_more = repository.read_public_analysis_event_page()

    assert first.accepted is True
    assert first.sequence == 1
    assert duplicate.duplicate is True
    assert duplicate.sequence == 1
    assert repository.latest_analysis_event_sequence() == 1
    assert repository.list_analysis_events() == [event.model_copy(update={"sequence": 1})]
    assert events == [event.model_copy(update={"sequence": 1})]
    assert (next_sequence, latest_sequence, has_more) == (1, 1, False)


def test_task_event_reuses_and_obeys_callers_transaction(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "events.sqlite")
    _initialize(factory)
    repository = SQLiteAnalysisEventStore(factory)

    connection = factory()
    calls_before_insert = factory.calls
    try:
        connection.execute("BEGIN IMMEDIATE")
        repository.insert_task_event(
            connection,
            job_id="job-rollback",
            status=GitHubJobStatus.RUNNING,
            attempt=1,
            deadline_at=datetime.now(timezone.utc).isoformat(),
        )
        assert factory.calls == calls_before_insert
        connection.rollback()
    finally:
        connection.close()

    assert repository.list_analysis_events() == []

    connection = factory()
    try:
        with connection:
            repository.insert_task_event(
                connection,
                job_id="job-commit",
                status=GitHubJobStatus.QUEUED,
                attempt=0,
            )
    finally:
        connection.close()

    persisted = repository.list_analysis_events()
    assert [event.event_type for event in persisted] == [AnalysisEventType.TASK_QUEUED]
    assert persisted[0].safe_metrics == {"job_status": "QUEUED"}


class _ConnectionFactory:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.calls = 0

    def __call__(self) -> sqlite3.Connection:
        self.calls += 1
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _initialize(factory: _ConnectionFactory) -> None:
    connection = factory()
    try:
        with connection:
            ensure_main_schema(connection)
    finally:
        connection.close()
