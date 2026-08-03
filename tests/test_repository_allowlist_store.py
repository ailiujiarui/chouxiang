from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from refactor_agent.repository_allowlist_store import SQLiteRepositoryAllowlistStore
from refactor_agent.store_schema import ensure_main_schema


def test_allowlist_repository_persists_entries_limits_and_audit_events(tmp_path: Path) -> None:
    factory = _ConnectionFactory(tmp_path / "allowlist.sqlite")
    _initialize(factory)
    repository = SQLiteRepositoryAllowlistStore(factory)

    created = repository.add_repository_allowlist_entry("octo/demo", max_entries=1)
    duplicate = repository.add_repository_allowlist_entry("octo/demo", max_entries=1)
    limited = repository.add_repository_allowlist_entry("octo/second", max_entries=1)

    assert created is not None
    assert duplicate == created
    assert limited is None
    assert repository.get_repository_allowlist_entry("octo/demo") == created
    assert repository.list_repository_allowlist_entries() == [created]
    assert repository.count_repository_allowlist_entries() == 1
    assert [event.action for event in repository.list_repository_allowlist_events()] == ["ADD"]

    assert repository.remove_repository_allowlist_entry("octo/demo") is True
    assert repository.remove_repository_allowlist_entry("octo/demo") is False
    assert repository.list_repository_allowlist_entries() == []
    assert [event.action for event in repository.list_repository_allowlist_events()] == [
        "ADD",
        "REMOVE",
    ]


def test_allowlist_entry_and_audit_event_rollback_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _ConnectionFactory(tmp_path / "allowlist.sqlite")
    _initialize(factory)
    repository = SQLiteRepositoryAllowlistStore(factory)

    def fail_audit_insert(
        connection: sqlite3.Connection,
        action: str,
        repo_full_name: str,
    ) -> None:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(repository, "_insert_event", fail_audit_insert)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        repository.add_repository_allowlist_entry("octo/demo")

    assert repository.get_repository_allowlist_entry("octo/demo") is None
    assert repository.list_repository_allowlist_events() == []


class _ConnectionFactory:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def __call__(self) -> sqlite3.Connection:
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
