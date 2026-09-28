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
