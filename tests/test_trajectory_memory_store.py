from __future__ import annotations

import sqlite3
from pathlib import Path

from refactor_agent.models import TrajectoryMemoryRecord
from refactor_agent.store_schema import ensure_main_schema
from refactor_agent.trajectory_memory_store import SQLiteTrajectoryMemoryStore


def test_trajectory_memory_repository_upserts_filters_and_orders_records(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "memory.sqlite")
    _initialize(factory)
    repository = SQLiteTrajectoryMemoryStore(factory)
    first = _memory(
        memory_id="memory-1",
        repo_name="octo/alpha",
        target_path="alpha.py",
        created_at="2026-08-01T01:00:00+00:00",
    )
    second = _memory(
        memory_id="memory-2",
        repo_name="octo/alpha",
        target_path="beta.py",
        created_at="2026-08-01T02:00:00+00:00",
    )
    third = _memory(
        memory_id="memory-3",
        repo_name="octo/beta",
        target_path="alpha.py",
        created_at="2026-08-01T03:00:00+00:00",
    )
    repository.save_memory(first)
    repository.save_memory(second)
    repository.save_memory(third)

    updated = first.model_copy(update={"lesson": "updated lesson", "reward": 4.5})
    repository.save_memory(updated)

    assert repository.list_memory(repo_name="octo/alpha") == [second, updated]
    assert repository.list_memory(target_path="alpha.py") == [third, updated]
    assert repository.list_memory("octo/alpha", "alpha.py") == [updated]
    assert repository.list_memory(limit=1) == [third]


def test_trajectory_memory_repository_generates_timestamp_and_closes_connections(
    tmp_path: Path,
) -> None:
    factory = _TrackingConnectionFactory(tmp_path / "memory.sqlite")
    _initialize(factory)
    repository = SQLiteTrajectoryMemoryStore(factory)
    record = _memory(memory_id="memory-generated", created_at=None)

    repository.save_memory(record)
    persisted = repository.list_memory()

    assert len(persisted) == 1
    assert persisted[0].created_at is not None
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


def _memory(
    *,
    memory_id: str,
    repo_name: str = "octo/demo",
    target_path: str = "app.py",
    created_at: str | None,
) -> TrajectoryMemoryRecord:
    return TrajectoryMemoryRecord(
        memory_id=memory_id,
        run_id=f"run-{memory_id}",
        repo_name=repo_name,
        target_path=target_path,
        status="SUCCESS",
        lesson="reuse the smallest safe change",
        reward=3.0,
        created_at=created_at,
    )
