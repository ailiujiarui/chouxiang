from __future__ import annotations

import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from nailong_agent.events import ActivityEvent, ActivityType, PetPreferences
from nailong_agent.notification_policy import NotificationPolicy
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.privacy import PrivacyConsent
from nailong_agent.privacy_store import PrivacyStore
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType
from refactor_agent.config import AppSettings
from refactor_agent.errors import is_database_locked
from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob, RepositoryJobKind
from refactor_agent.sqlite_runtime import SQLitePolicy, connect_sqlite, wal_runtime_is_safe
from refactor_agent.store import JobTransitionError, SQLiteRunStore
from refactor_agent.webhook import create_app


@pytest.fixture(params=["delete", "wal"])
def concurrent_policy(request: pytest.FixtureRequest) -> SQLitePolicy:
    mode = str(request.param)
    if mode == "wal" and not wal_runtime_is_safe():
        pytest.skip("WAL concurrency requires a SQLite runtime accepted by the safety gate")
    return SQLitePolicy(busy_timeout_ms=1_000, journal_mode=mode)


def test_short_contention_waits_then_commits_exactly_once(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "short-contention.sqlite"
    first = SQLiteRunStore(database, policy=concurrent_policy)
    second = SQLiteRunStore(database, policy=concurrent_policy)
    locked = threading.Event()
    release = threading.Event()
    waiter_started = threading.Event()
    waiter_done = threading.Event()

    def hold_lock() -> None:
        with first._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO repository_allowlist (repo_full_name, created_at) VALUES (?, ?)",
                ("octo/holder", "2026-07-30T00:00:00+00:00"),
            )
            locked.set()
            assert release.wait(5)

    def wait_for_lock() -> None:
        waiter_started.set()
        second.add_repository_allowlist_entry("octo/waiter")
        waiter_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        assert locked.wait(5)
        waiter = executor.submit(wait_for_lock)
        assert waiter_started.wait(5)
        assert not waiter_done.wait(0.05)
        release.set()
        holder.result(timeout=5)
        waiter.result(timeout=5)

    assert [row.repo_full_name for row in first.list_repository_allowlist_entries()] == [
        "octo/holder",
        "octo/waiter",
    ]


def test_lock_timeout_is_bounded_and_has_no_partial_domain_write(tmp_path: Path) -> None:
    database = tmp_path / "timeout.sqlite"
    policy = SQLitePolicy(busy_timeout_ms=120, journal_mode="delete")
    holder_store = SQLiteRunStore(database, policy=policy)
    waiting_store = SQLiteRunStore(database, policy=policy)
    locked = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder_store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_lock)
        assert locked.wait(5)
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as caught:
            waiting_store.add_repository_allowlist_entry("octo/timeout")
        elapsed = time.monotonic() - started
        release.set()
        holder.result(timeout=5)

    assert is_database_locked(caught.value)
    assert 0.08 <= elapsed < 0.8
    assert waiting_store.get_repository_allowlist_entry("octo/timeout") is None
    assert "wait_exhausted=true" in waiting_store.sqlite_diagnostics.locked_summary("allowlist")



def test_concurrent_duplicate_submissions_converge_without_overwriting_lease(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "duplicate.sqlite"
    first_store = SQLiteRunStore(database, policy=concurrent_policy)
    second_store = SQLiteRunStore(database, policy=concurrent_policy)
    first_job = _job("duplicate-a", "same-delivery", issue_number=90)
    second_job = _job("duplicate-b", "same-delivery", issue_number=90)

    first, second = _run_concurrently(
        lambda: first_store.create_github_job(first_job),
        lambda: second_store.create_github_job(second_job),
    )

    assert first.job_id == second.job_id
    assert len(first_store.list_github_jobs()) == 1
    claimed = first_store.claim_next_github_job("lease-owner", 30, 3)
    assert claimed is not None
    duplicate = second_store.create_github_job(first_job if claimed.job_id == first_job.job_id else second_job)
    assert duplicate.status.value == "RUNNING"
    assert duplicate.lease_owner == "lease-owner"
    assert [event.event_type for event in first_store.list_job_events(claimed.job_id)].count("JOB_CREATED") == 1






def test_privacy_append_clear_and_consent_update_serialize_cleanly(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "privacy.sqlite"
    first = PrivacyStore(database, policy=concurrent_policy)
    second = PrivacyStore(database, policy=concurrent_policy)
    third = PrivacyStore(database, policy=concurrent_policy)
    first.save_consent(PrivacyConsent(activity_collection_enabled=False, decision_recorded=True))
    event = ActivityEvent(
        event_id="privacy-race",
        source="window",
        application_id="code",
        activity=ActivityType.CODING,
        confidence=0.9,
    )
    expected_consent = PrivacyConsent(
        activity_collection_enabled=True,
        remote_inference_enabled=True,
        decision_recorded=True,
    )

    _run_concurrently(
        lambda: first.append_minimized_activity(event),
        second.clear_activity_history,
        lambda: third.save_consent(expected_consent),
    )

    assert first.activity_count() in {0, 1}
    assert first.activity_window_count() == 0
    assert first.load_consent() == expected_consent






def _run_concurrently(*operations: Callable[[], object]) -> list[object]:
    barrier = threading.Barrier(len(operations) + 1)

    def invoke(operation: Callable[[], object]) -> object:
        barrier.wait(timeout=5)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        barrier.wait(timeout=5)
        return [future.result(timeout=10) for future in futures]


def _settings(tmp_path: Path, database: Path, policy: SQLitePolicy) -> AppSettings:
    return AppSettings(
        admin_token="admin-secret",
        allowed_repositories={"octo/demo"},
        sandbox_backend="docker",
        mock_llm=True,
        run_root=tmp_path / "runs",
        database_path=database,
        sqlite_policy=policy,
    )


def _job(job_id: str, delivery_id: str, *, issue_number: int = 42) -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_kind=RepositoryJobKind.GITHUB_WEBHOOK,
        job_id=job_id,
        delivery_id=delivery_id,
        repo_full_name="octo/demo",
        issue_number=issue_number,
        issue_title="Concurrency",
        issue_text="target: app.py",
        target_path="app.py",
        tests_path="tests",
        event_name="issues",
        action="opened",
    )


def _analysis_event(sequence: int, task_id: str, now: datetime) -> AnalysisEvent:
    return AnalysisEvent(
        sequence=sequence,
        event_id=f"notification-event-{sequence}",
        event_type=AnalysisEventType.TASK_FAILED,
        task_id=task_id,
        source="worker",
        occurred_at=now,
    )


def _notification_service(database: Path, policy: SQLitePolicy, now: datetime) -> NotificationService:
    return NotificationService(
        store=NotificationStore(database, policy=policy),
        policy=NotificationPolicy(minimum_cooldown_seconds=0, maximum_cooldown_seconds=0),
        clock=lambda: now,
        minimum_popup_start_spacing_seconds=0,
    )


def _cross_process_snapshot_reader(
    database: str,
    ready,
    release,
    output,
) -> None:
    policy = SQLitePolicy(busy_timeout_ms=2_000, journal_mode="wal")
    with connect_sqlite(Path(database), policy) as connection:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        connection.execute("BEGIN")
        before = int(connection.execute("SELECT COUNT(*) FROM repository_allowlist").fetchone()[0])
        ready.set()
        if not release.wait(10):
            raise RuntimeError("parent did not release cross-process reader")
        during = int(connection.execute("SELECT COUNT(*) FROM repository_allowlist").fetchone()[0])
        output.put((mode, before, during))
