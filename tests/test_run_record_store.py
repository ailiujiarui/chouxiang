from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from refactor_agent.models import BenchmarkCaseRecord, BenchmarkRunRecord, RunRecord
from refactor_agent.run_record_store import SQLiteRunRecordStore
from refactor_agent.store_schema import ensure_main_schema


def test_run_record_repository_round_trips_upserts_and_sanitizes_snapshots(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "records.sqlite")
    _initialize(factory)
    repository = SQLiteRunRecordStore(factory)
    failed = RunRecord(
        run_id="run-1",
        repo_name="octo/demo",
        self_heal_count=1,
        status="FAILED",
        error="raw legacy error",
        error_summary="Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    )

    repository.save(failed)
    persisted = repository.get(failed.run_id)

    assert persisted is not None
    assert persisted.error is None
    assert persisted.error_summary == "Authorization: Bearer [REDACTED]"

    succeeded = failed.model_copy(
        update={
            "status": "SUCCESS",
            "self_heal_count": 2,
            "error": None,
            "error_summary": None,
        }
    )
    repository.save(succeeded)

    assert repository.get(succeeded.run_id) == succeeded
    assert repository.list_runs() == [succeeded]


def test_benchmark_header_and_case_set_replace_atomically(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "records.sqlite")
    _initialize(factory)
    repository = SQLiteRunRecordStore(factory)
    run = _benchmark_run()
    case = _benchmark_case()
    repository.save_benchmark_run(run, [case])

    replacement = run.model_copy(update={"status": "FAILED"})
    invalid_case = case.model_copy(update={"run_id": "missing-parent"})
    with pytest.raises(sqlite3.IntegrityError):
        repository.save_benchmark_run(replacement, [invalid_case])

    assert repository.get_benchmark_run(run.run_id) == run
    assert repository.list_benchmark_runs() == [run]
    assert repository.list_benchmark_case_results(run.run_id) == [case]


def test_run_record_repository_closes_every_operation_connection(tmp_path: Path) -> None:
    factory = _TrackingConnectionFactory(tmp_path / "records.sqlite")
    _initialize(factory)
    repository = SQLiteRunRecordStore(factory)
    record = RunRecord(
        run_id="run-closed",
        repo_name="octo/demo",
        self_heal_count=0,
        status="SUCCESS",
    )

    repository.save(record)
    assert repository.get(record.run_id) == record
    assert repository.list_runs() == [record]

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


def _benchmark_run() -> BenchmarkRunRecord:
    return BenchmarkRunRecord(
        run_id="benchmark-1",
        manifest_hash="a" * 64,
        provider="mock",
        model="deterministic-gold",
        status="SUCCESS",
        generated_at="2026-07-14T00:00:00+00:00",
    )


def _benchmark_case() -> BenchmarkCaseRecord:
    return BenchmarkCaseRecord(
        run_id="benchmark-1",
        case_name="off-by-one",
        repository="octo/demo",
        commit="b" * 40,
        provider="mock",
        model="deterministic-gold",
        status="SUCCESS",
        expected_status="SUCCESS",
        normalized_hash="c" * 64,
    )
