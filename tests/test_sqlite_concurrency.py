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


def test_api_submission_and_worker_claim_overlap(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "api-worker.sqlite"
    api_store = SQLiteRunStore(database, policy=concurrent_policy)
    worker_store = SQLiteRunStore(database, policy=concurrent_policy)
    settings = _settings(tmp_path, database, concurrent_policy)
    app = create_app(settings=settings, store=api_store, start_worker=False)
    submitted = threading.Event()

    def submit():
        with TestClient(app) as client:
            response = client.post(
                "/jobs/snippet",
                headers={"Authorization": "Bearer admin-secret"},
                json={
                    "source": "def add(a, b):\n    return a + b\n",
                    "refactor_request": "review",
                    "mode": "REVIEW",
                    "persona": "STRICT",
                },
            )
        submitted.set()
        return response

    def claim():
        record = worker_store.claim_next_github_job("worker-api", 30, 3)
        if record is None:
            assert submitted.wait(5)
            record = worker_store.claim_next_github_job("worker-api", 30, 3)
        return record

    response, claimed = _run_concurrently(submit, claim)

    assert response.status_code == 202
    assert claimed is not None
    assert claimed.job_id == response.json()["job_id"]
    assert claimed.lease_owner == "worker-api"
    assert [event.event_type for event in worker_store.list_job_events(claimed.job_id)] == [
        "JOB_CREATED",
        "STATE_TRANSITION",
    ]


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


def test_cancellation_racing_heartbeat_cannot_be_erased(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "cancel-heartbeat.sqlite"
    owner_store = SQLiteRunStore(database, policy=concurrent_policy)
    api_store = SQLiteRunStore(database, policy=concurrent_policy)
    heartbeat_store = SQLiteRunStore(database, policy=concurrent_policy)
    job = _job("cancel-race", "cancel-race-delivery")
    owner_store.create_github_job(job)
    assert owner_store.claim_next_github_job("worker-a", 30, 3) is not None

    cancellation, renewed = _run_concurrently(
        lambda: api_store.request_github_job_cancellation(job.job_id),
        lambda: heartbeat_store.renew_github_job_lease(job.job_id, "worker-a", 30),
    )

    final = owner_store.get_github_job(job.job_id)
    assert final is not None
    assert final.status.value == "CANCEL_REQUESTED"
    assert cancellation[0].status.value == "CANCEL_REQUESTED"
    assert renewed in {True, False}
    assert owner_store.renew_github_job_lease(job.job_id, "worker-a", 30) is False


def test_completion_racing_expired_lease_recovery_has_one_legal_owner(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "completion-recovery.sqlite"
    owner_store = SQLiteRunStore(database, policy=concurrent_policy)
    recovery_store = SQLiteRunStore(database, policy=concurrent_policy)
    job = _job("completion-race", "completion-race-delivery")
    owner_store.create_github_job(job)
    assert owner_store.claim_next_github_job("worker-old", 30, 3) is not None
    with owner_store._connect() as connection:
        connection.execute(
            "UPDATE github_jobs SET lease_expires_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job.job_id),
        )

    def complete():
        try:
            return owner_store.complete_github_job(
                job,
                GitHubAutomationResult(
                    job_id=job.job_id,
                    repo_full_name=job.repo_full_name,
                    issue_number=job.issue_number,
                    status="SUCCESS",
                ),
                worker_id="worker-old",
            )
        except JobTransitionError:
            return "lease-lost"

    completion, reclaimed = _run_concurrently(
        complete,
        lambda: recovery_store.claim_next_github_job("worker-new", 30, 3),
    )
    final = owner_store.get_github_job(job.job_id)
    assert final is not None
    assert (final.status.value, final.lease_owner) in {
        ("SUCCESS", None),
        ("RUNNING", "worker-new"),
    }
    if final.status.value == "SUCCESS":
        assert completion != "lease-lost"
        assert reclaimed is None
    else:
        assert completion == "lease-lost"
        assert reclaimed is not None


def test_event_emit_and_heartbeat_overlap_api_snapshot_read(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "event-heartbeat-read.sqlite"
    store = SQLiteRunStore(database, policy=concurrent_policy)
    event_store = SQLiteRunStore(database, policy=concurrent_policy)
    heartbeat_store = SQLiteRunStore(database, policy=concurrent_policy)
    reader_store = SQLiteRunStore(database, policy=concurrent_policy)
    job = _job("event-race", "event-race-delivery")
    store.create_github_job(job)
    assert store.claim_next_github_job("worker-events", 30, 3) is not None
    event = AnalysisEvent(
        event_id="event-concurrent",
        event_type=AnalysisEventType.PHASE_STARTED,
        task_id=job.job_id,
        source="orchestrator",
        phase="analysis",
    )

    receipt, renewed, page = _run_concurrently(
        lambda: event_store.emit(event),
        lambda: heartbeat_store.renew_github_job_lease(job.job_id, "worker-events", 30),
        lambda: reader_store.read_public_analysis_event_page(after=0, limit=100),
    )

    assert receipt.accepted and not receipt.duplicate
    assert renewed is True
    page_sequences = [item.sequence for item in page[0]]
    assert page_sequences == sorted(page_sequences)
    final_events = store.list_analysis_events(after=0, limit=100)
    final_sequences = [item.sequence for item in final_events]
    assert len(final_sequences) == len(set(final_sequences))
    assert any(item.event_id == "event-concurrent" for item in final_events)


def test_notification_ingest_delivery_ack_and_settings_overlap(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    database = tmp_path / "notifications.sqlite"
    now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    seed_service = _notification_service(database, concurrent_policy, now)
    seeded = seed_service.ingest_analysis_event(_analysis_event(1, "seed", now))
    assert seeded.notification_id is not None
    ingest_service = _notification_service(database, concurrent_policy, now)
    delivery_service = _notification_service(database, concurrent_policy, now)
    dnd_service = _notification_service(database, concurrent_policy, now)
    settings_store = NotificationStore(database, policy=concurrent_policy)

    def lease_and_ack() -> tuple[str | None, bool]:
        intent = delivery_service.lease_next()
        if intent is None:
            return None, False
        return intent.notification_id, delivery_service.acknowledge(intent.notification_id, "shown")

    receipt, acknowledgement, _, _ = _run_concurrently(
        lambda: ingest_service.ingest_analysis_event(_analysis_event(2, "second", now)),
        lease_and_ack,
        lambda: dnd_service.set_do_not_disturb(True),
        lambda: settings_store.save_preferences(PetPreferences(manual_pause_enabled=True)),
    )

    status = seed_service.get_status()
    intents = seed_service.store.list_intents()
    assert receipt.accepted
    assert status.last_consumed_sequence == 2
    assert status.do_not_disturb is True
    assert status.manual_pause_enabled is True
    assert len({intent.dedupe_key for intent in intents}) == len(intents)
    assert status.remaining_daily_popup_budget in {11, 12}
    assert acknowledgement[1] in {True, False}
    if acknowledgement[1]:
        assert acknowledgement[0] is not None
        assert delivery_service.acknowledge(acknowledgement[0], "shown") is False


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


def test_three_independent_databases_hold_write_transactions_together(
    tmp_path: Path,
    concurrent_policy: SQLitePolicy,
) -> None:
    main = SQLiteRunStore(tmp_path / "main.sqlite", policy=concurrent_policy)
    notifications = NotificationStore(tmp_path / "notifications.sqlite", policy=concurrent_policy)
    privacy = PrivacyStore(tmp_path / "privacy.sqlite", policy=concurrent_policy)
    acquired = threading.Barrier(3)

    def main_write() -> None:
        with main._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO repository_allowlist (repo_full_name, created_at) VALUES (?, ?)",
                ("octo/independent", "2026-07-30T00:00:00+00:00"),
            )
            acquired.wait(timeout=5)

    def notification_write() -> None:
        with notifications._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE notification_runtime SET do_not_disturb = 1 WHERE id = 1")
            acquired.wait(timeout=5)

    def privacy_write() -> None:
        with privacy._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO pet_activity_events
                    (event_id, occurred_at, source, application_id, idle_seconds,
                     is_fullscreen, is_meeting_likely, activity, confidence, summary)
                VALUES (?, ?, ?, ?, NULL, 0, 0, ?, ?, NULL)
                """,
                (
                    "independent-activity",
                    "2026-07-30T00:00:00+00:00",
                    "window",
                    "code",
                    "coding",
                    0.9,
                ),
            )
            acquired.wait(timeout=5)

    _run_concurrently(main_write, notification_write, privacy_write)

    assert main.get_repository_allowlist_entry("octo/independent") is not None
    assert notifications.status().do_not_disturb is True
    assert privacy.activity_count() == 1


@pytest.mark.skipif(not wal_runtime_is_safe(), reason="requires WAL-safe SQLite runtime")
def test_wal_reader_keeps_stable_snapshot_while_writer_commits(tmp_path: Path) -> None:
    policy = SQLitePolicy(busy_timeout_ms=1_000, journal_mode="wal")
    database = tmp_path / "wal-snapshot.sqlite"
    store = SQLiteRunStore(database, policy=policy)
    writer = SQLiteRunStore(database, policy=policy)

    with store._connect() as reader:
        reader.execute("BEGIN")
        before = reader.execute("SELECT COUNT(*) FROM repository_allowlist").fetchone()[0]
        with ThreadPoolExecutor(max_workers=1) as executor:
            committed = executor.submit(writer.add_repository_allowlist_entry, "octo/wal")
            assert committed.result(timeout=5) is not None
        during = reader.execute("SELECT COUNT(*) FROM repository_allowlist").fetchone()[0]
    after = store.count_repository_allowlist_entries()

    assert before == during == 0
    assert after == 1


@pytest.mark.skipif(not wal_runtime_is_safe(), reason="requires WAL-safe SQLite runtime")
def test_same_host_cross_process_reader_and_writer_use_wal_snapshot(tmp_path: Path) -> None:
    policy = SQLitePolicy(busy_timeout_ms=2_000, journal_mode="wal")
    database = tmp_path / "wal-process.sqlite"
    store = SQLiteRunStore(database, policy=policy)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    output = context.Queue()
    process = context.Process(
        target=_cross_process_snapshot_reader,
        args=(str(database), ready, release, output),
    )
    process.start()
    try:
        assert ready.wait(10)
        assert store.add_repository_allowlist_entry("octo/process") is not None
        release.set()
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert output.get(timeout=2) == ("wal", 0, 0)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert store.count_repository_allowlist_entries() == 1


def test_bounded_repeated_contention_preserves_keys_state_and_event_order(tmp_path: Path) -> None:
    policy = SQLitePolicy(busy_timeout_ms=1_000, journal_mode="delete")
    database = tmp_path / "stress.sqlite"
    stores = [SQLiteRunStore(database, policy=policy) for _ in range(6)]
    jobs = [_job(f"stress-{index}", f"delivery-{index}", issue_number=777) for index in range(12)]

    with ThreadPoolExecutor(max_workers=12) as executor:
        records = list(executor.map(lambda pair: pair[0].create_github_job(pair[1]), zip(stores * 2, jobs)))
    assert len({record.job_id for record in records}) == 1
    canonical_job_id = records[0].job_id
    claims = _run_concurrently(
        *(lambda index=index: stores[index].claim_next_github_job(f"worker-{index}", 30, 3) for index in range(6))
    )
    claimed = [record for record in claims if record is not None]
    assert len(claimed) == 1
    assert claimed[0].job_id == canonical_job_id

    events = [
        AnalysisEvent(
            event_id=f"stress-event-{index}",
            event_type=AnalysisEventType.PHASE_STARTED,
            task_id=canonical_job_id,
            source="orchestrator",
            phase="analysis",
        )
        for index in range(20)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(lambda pair: pair[0].emit(pair[1]), zip(stores * 4, events)))
    assert all(receipt.accepted and not receipt.duplicate for receipt in receipts)
    persisted = stores[0].list_analysis_events(after=0, limit=100)
    sequences = [event.sequence for event in persisted]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert len([event for event in persisted if event.event_id.startswith("stress-event-")]) == 20
    job_events = stores[0].list_job_events(canonical_job_id)
    assert [event.event_type for event in job_events] == ["JOB_CREATED", "STATE_TRANSITION"]


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
