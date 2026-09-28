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



def test_forced_wal_fails_closed_on_unsafe_runtime(tmp_path: Path) -> None:
    with pytest.raises(SQLiteConfigurationError, match="wal_gate_failed:sqlite_version_not_fixed"):
        initialize_sqlite_database(
            tmp_path / "forced.sqlite",
            SQLitePolicy(journal_mode="wal"),
            version_info=(3, 50, 4),
            filesystem_local=True,
        )






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
