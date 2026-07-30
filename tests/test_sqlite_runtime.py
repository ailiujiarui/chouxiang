from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nailong_agent.config import NailongSettings
from nailong_agent.notification_store import NotificationStore
from nailong_agent.privacy_store import PrivacyStore
from refactor_agent.config import AppSettings
from refactor_agent.sqlite_runtime import (
    SQLiteConfigurationError,
    SQLitePolicy,
    connect_sqlite,
    initialize_sqlite_database,
    wal_runtime_is_safe,
)
from refactor_agent.store import SQLiteRunStore


def test_policy_validates_mode_and_timeout() -> None:
    assert SQLitePolicy().busy_timeout_ms == 5_000
    assert SQLitePolicy(journal_mode="WAL").journal_mode == "wal"
    with pytest.raises(ValueError, match="journal_mode"):
        SQLitePolicy(journal_mode="truncate")
    with pytest.raises(ValueError, match="positive"):
        SQLitePolicy(busy_timeout_ms=0)


def test_app_and_nailong_settings_share_environment_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFACTOR_AGENT_SQLITE_BUSY_TIMEOUT_MS", "4321")
    monkeypatch.setenv("REFACTOR_AGENT_SQLITE_JOURNAL_MODE", "delete")

    assert AppSettings.from_env().sqlite_policy == SQLitePolicy(4321, "delete")
    assert NailongSettings.from_env().sqlite_policy == SQLitePolicy(4321, "delete")


@pytest.mark.parametrize(
    "store_type,filename",
    [
        (SQLiteRunStore, "main.sqlite"),
        (NotificationStore, "notification.sqlite"),
        (PrivacyStore, "privacy.sqlite"),
    ],
)
def test_all_stores_apply_the_injected_connection_policy(
    tmp_path: Path,
    store_type,
    filename: str,
) -> None:
    policy = SQLitePolicy(busy_timeout_ms=137, journal_mode="delete")
    store = store_type(tmp_path / filename, policy=policy)

    with store._connect() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 137
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        row = connection.execute("SELECT 7 AS value").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["value"] == 7
    assert store.sqlite_diagnostics.actual_journal_mode == "delete"
    assert store.sqlite_diagnostics.busy_timeout_ms == 137


def test_auto_mode_falls_back_on_an_unfixed_runtime(tmp_path: Path) -> None:
    diagnostics = initialize_sqlite_database(
        tmp_path / "auto.sqlite",
        SQLitePolicy(journal_mode="auto"),
        version_info=(3, 50, 4),
        filesystem_local=True,
    )

    assert diagnostics.actual_journal_mode == "delete"
    assert diagnostics.wal_safe is False
    assert diagnostics.wal_gate_reason == "sqlite_version_not_fixed"


def test_auto_mode_enables_and_verifies_wal_on_a_safe_runtime(tmp_path: Path) -> None:
    diagnostics = initialize_sqlite_database(
        tmp_path / "auto-wal.sqlite",
        SQLitePolicy(journal_mode="auto"),
        version_info=(3, 51, 3),
        filesystem_local=True,
    )

    assert diagnostics.actual_journal_mode == "wal"
    assert diagnostics.wal_safe is True


def test_forced_wal_fails_closed_on_unsafe_runtime(tmp_path: Path) -> None:
    with pytest.raises(SQLiteConfigurationError, match="wal_gate_failed:sqlite_version_not_fixed"):
        initialize_sqlite_database(
            tmp_path / "forced.sqlite",
            SQLitePolicy(journal_mode="wal"),
            version_info=(3, 50, 4),
            filesystem_local=True,
        )


def test_unsafe_runtime_cannot_join_an_existing_wal_database(tmp_path: Path) -> None:
    database = tmp_path / "existing-wal.sqlite"
    initialize_sqlite_database(
        database,
        SQLitePolicy(journal_mode="wal"),
        version_info=(3, 51, 3),
        filesystem_local=True,
    )

    with pytest.raises(SQLiteConfigurationError, match="unsafe_runtime_existing_wal"):
        initialize_sqlite_database(
            database,
            SQLitePolicy(journal_mode="auto"),
            version_info=(3, 50, 4),
            filesystem_local=True,
        )


def test_delete_mode_is_a_controlled_wal_rollback(tmp_path: Path) -> None:
    database = tmp_path / "rollback.sqlite"
    initialize_sqlite_database(
        database,
        SQLitePolicy(journal_mode="wal"),
        version_info=(3, 51, 3),
        filesystem_local=True,
    )
    diagnostics = initialize_sqlite_database(
        database,
        SQLitePolicy(journal_mode="delete"),
        version_info=(3, 50, 4),
        filesystem_local=True,
    )

    assert diagnostics.actual_journal_mode == "delete"


def test_wal_restart_checkpoint_and_delete_rollback_preserve_data(tmp_path: Path) -> None:
    database = tmp_path / "wal-lifecycle.sqlite"
    wal_policy = SQLitePolicy(journal_mode="wal")
    initialize_sqlite_database(
        database,
        wal_policy,
        version_info=(3, 51, 3),
        filesystem_local=True,
    )
    with closing(connect_sqlite(database, wal_policy)) as connection, connection:
        connection.execute("CREATE TABLE lifecycle (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO lifecycle (value) VALUES ('persisted')")

    with closing(connect_sqlite(database, wal_policy)) as reopened, reopened:
        assert reopened.execute("SELECT value FROM lifecycle").fetchone()[0] == "persisted"
        checkpoint = reopened.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint[0] == 0

    diagnostics = initialize_sqlite_database(
        database,
        SQLitePolicy(journal_mode="delete"),
        version_info=(3, 50, 4),
        filesystem_local=True,
    )
    with closing(connect_sqlite(database, SQLitePolicy(journal_mode="delete"))) as connection, connection:
        assert connection.execute("SELECT value FROM lifecycle").fetchone()[0] == "persisted"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert diagnostics.actual_journal_mode == "delete"


@pytest.mark.parametrize(
    "version,expected",
    [
        ((3, 44, 5), False),
        ((3, 44, 6), True),
        ((3, 49, 99), False),
        ((3, 50, 6), False),
        ((3, 50, 7), True),
        ((3, 51, 2), False),
        ((3, 51, 3), True),
        ((3, 52, 0), True),
    ],
)
def test_wal_runtime_gate_recognizes_only_fixed_release_lines(
    version: tuple[int, int, int],
    expected: bool,
) -> None:
    assert wal_runtime_is_safe(version) is expected


def test_public_diagnostics_and_lock_summary_do_not_expose_path_or_sql(tmp_path: Path) -> None:
    database = tmp_path / "secret-name.sqlite"
    diagnostics = initialize_sqlite_database(
        database,
        SQLitePolicy(journal_mode="delete"),
        version_info=(3, 50, 4),
        filesystem_local=True,
    )

    rendered = str(diagnostics.as_public_dict()) + diagnostics.locked_summary("worker job; SELECT *")
    assert str(database) not in rendered
    assert "SELECT" not in rendered
    assert "wait_exhausted=true" in rendered
