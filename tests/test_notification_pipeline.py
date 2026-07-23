from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import httpx

from nailong_agent.analysis_subscriber import AnalysisEventSubscriber, HttpxSSEAnalysisEventSource
from nailong_agent.events import NotificationKind
from nailong_agent.notification_policy import NotificationPolicy
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def test_regular_notifications_use_relaxed_cooldown_without_a_count_cap(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)

    started = service.ingest_analysis_event(_event(1, AnalysisEventType.TASK_STARTED, "task-1", clock()))
    clock.advance(seconds=1)
    cooled = service.ingest_analysis_event(_event(2, AnalysisEventType.PYTEST_PASSED, "task-1", clock()))
    clock.advance(seconds=299)
    ready = service.ingest_analysis_event(_event(3, AnalysisEventType.PYTEST_PASSED, "task-1", clock()))

    assert started.notification_id is not None
    assert cooled.reason == "regular_cooldown"
    assert ready.notification_id is not None
    assert [intent.kind for intent in service.store.list_intents()] == [
        NotificationKind.ENCOURAGEMENT,
        NotificationKind.PYTEST_CELEBRATION,
    ]

    for sequence in range(10, 30):
        service.ingest_analysis_event(
            _event(sequence, AnalysisEventType.TASK_FAILED, f"terminal-{sequence}", clock())
        )
    assert service.get_status().pending_count == 22


def test_pytest_and_final_verdict_have_distinct_celebrations_and_terminal_dedupes(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    service.ingest_analysis_event(_event(1, AnalysisEventType.TASK_STARTED, "task-1", clock()))
    clock.advance(seconds=300)
    pytest_receipt = service.ingest_analysis_event(
        _event(2, AnalysisEventType.PYTEST_PASSED, "task-1", clock())
    )
    final_receipt = service.ingest_analysis_event(
        _event(3, AnalysisEventType.FINAL_VERDICT_PASSED, "task-1", clock())
    )
    duplicate_terminal = service.ingest_analysis_event(
        _event(4, AnalysisEventType.TASK_COMPLETED, "task-1", clock())
    )

    intents = service.store.list_intents()
    pytest_intent = next(intent for intent in intents if intent.notification_id == pytest_receipt.notification_id)
    final_intent = next(intent for intent in intents if intent.notification_id == final_receipt.notification_id)
    assert pytest_intent.kind == NotificationKind.PYTEST_CELEBRATION
    assert final_intent.kind == NotificationKind.FINAL_CELEBRATION
    assert pytest_intent.message != final_intent.message
    assert "最终裁决" in final_intent.message
    assert duplicate_terminal.reason == "terminal_already_recorded"


def test_all_day_dnd_suppresses_popups_and_emits_one_terminal_summary_when_disabled(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    service.set_do_not_disturb(True)

    service.ingest_analysis_event(_event(1, AnalysisEventType.TASK_STARTED, "task-1", clock()))
    service.ingest_analysis_event(_event(2, AnalysisEventType.FINAL_VERDICT_PASSED, "task-1", clock()))
    service.ingest_analysis_event(_event(3, AnalysisEventType.TASK_COMPLETED, "task-1", clock()))
    service.ingest_analysis_event(_event(4, AnalysisEventType.TASK_FAILED, "task-2", clock()))

    assert service.lease_next() is None
    assert service.get_status().suppressed_terminal_count == 2
    summary = service.set_do_not_disturb(False)

    assert summary is not None
    assert summary.kind == NotificationKind.QUIET_MODE_SUMMARY
    assert "2 个任务" in summary.message
    assert service.get_status().suppressed_terminal_count == 0
    assert len([intent for intent in service.store.list_intents() if intent.kind == NotificationKind.QUIET_MODE_SUMMARY]) == 1


def test_pending_quiet_summary_is_not_counted_as_a_new_terminal_when_dnd_is_reenabled(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    service.set_do_not_disturb(True)
    service.ingest_analysis_event(_event(1, AnalysisEventType.TASK_FAILED, "task-1", clock()))
    assert service.set_do_not_disturb(False) is not None

    service.set_do_not_disturb(True)
    assert service.get_status().suppressed_terminal_count == 0
    assert service.set_do_not_disturb(False) is None
    assert len(
        [intent for intent in service.store.list_intents() if intent.kind == NotificationKind.QUIET_MODE_SUMMARY]
    ) == 1


def test_terminal_intents_bypass_regular_cooldown_but_popup_starts_are_serialized(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    service.ingest_analysis_event(_event(1, AnalysisEventType.TASK_STARTED, "ordinary", clock()))
    service.ingest_analysis_event(_event(2, AnalysisEventType.TASK_FAILED, "task-1", clock()))
    service.ingest_analysis_event(_event(3, AnalysisEventType.TASK_TIMED_OUT, "task-2", clock()))

    first = service.lease_next()
    assert first is not None and first.terminal is True
    clock.advance(seconds=29)
    assert service.lease_next() is None
    clock.advance(seconds=1)
    second = service.lease_next()
    assert second is not None and second.terminal is True
    assert second.notification_id != first.notification_id
    assert service.acknowledge(second.notification_id, "shown") is True


def test_long_task_reminder_fires_once_at_one_third_of_deadline(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    deadline = clock() + timedelta(seconds=900)
    service.ingest_analysis_event(
        _event(1, AnalysisEventType.TASK_STARTED, "task-1", clock(), deadline_at=deadline)
    )

    clock.advance(seconds=299)
    assert service.poll_long_tasks() is None
    clock.advance(seconds=1)
    reminder = service.poll_long_tasks()
    assert reminder is not None
    assert reminder.kind == NotificationKind.LONG_TASK_REMINDER
    clock.advance(seconds=300)
    assert service.poll_long_tasks() is None


def test_final_verdict_closes_long_task_before_the_worker_terminal_event(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    service.ingest_analysis_event(
        _event(
            1,
            AnalysisEventType.TASK_STARTED,
            "task-1",
            clock(),
            deadline_at=clock() + timedelta(seconds=900),
        )
    )
    service.ingest_analysis_event(
        _event(2, AnalysisEventType.FINAL_VERDICT_PASSED, "task-1", clock())
    )

    clock.advance(seconds=300)
    assert service.poll_long_tasks() is None


def test_manual_retry_starts_a_new_terminal_and_long_reminder_lifecycle(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    deadline = clock() + timedelta(seconds=900)
    service.ingest_analysis_event(
        _event(1, AnalysisEventType.TASK_STARTED, "task-1", clock(), deadline_at=deadline)
    )
    clock.advance(seconds=300)
    assert service.poll_long_tasks() is not None
    service.ingest_analysis_event(_event(2, AnalysisEventType.TASK_FAILED, "task-1", clock()))

    service.ingest_analysis_event(_event(3, AnalysisEventType.TASK_QUEUED, "task-1", clock()))
    service.ingest_analysis_event(
        _event(
            4,
            AnalysisEventType.TASK_STARTED,
            "task-1",
            clock(),
            deadline_at=clock() + timedelta(seconds=900),
        )
    )
    clock.advance(seconds=300)
    assert service.poll_long_tasks() is not None
    service.ingest_analysis_event(_event(5, AnalysisEventType.TASK_FAILED, "task-1", clock()))

    intents = service.store.list_intents()
    assert len([intent for intent in intents if intent.kind == NotificationKind.LONG_TASK_REMINDER]) == 2
    assert len([intent for intent in intents if intent.kind == NotificationKind.TERMINAL_FAILURE]) == 2


def test_subscriber_resumes_from_durable_cursor_and_ingestion_is_idempotent(tmp_path: Path) -> None:
    clock, service = _service(tmp_path)
    service.ingest_analysis_event(_event(2, AnalysisEventType.TASK_QUEUED, "task-1", clock()))
    streamed = _event(3, AnalysisEventType.PYTEST_PASSED, "task-1", clock())

    class FakeSource:
        after_values: list[int] = []

        def iter_events(self, *, after: int, stop: Event):
            self.after_values.append(after)
            yield streamed
            yield streamed

    source = FakeSource()
    subscriber = AnalysisEventSubscriber(source=source, notifications=service)

    assert subscriber.consume_once() == 2
    assert source.after_values == [2]
    assert service.get_status().last_consumed_sequence == 3
    assert len(service.store.list_intents()) == 1


def test_httpx_sse_source_sends_last_event_id_and_parses_analysis_event() -> None:
    captured_headers: list[str | None] = []
    payload = _event(7, AnalysisEventType.TASK_STARTED, "task-7", _now()).model_dump_json()

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers.get("Last-Event-ID"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"retry: 1000\n\nid: 7\nevent: analysis_event\ndata: {payload}\n\n",
        )

    source = HttpxSSEAnalysisEventSource(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    events = list(source.iter_events(after=6, stop=Event()))

    assert [event.sequence for event in events] == [7]
    assert captured_headers == ["6"]


def _service(tmp_path: Path) -> tuple[MutableClock, NotificationService]:
    clock = MutableClock(_now())
    service = NotificationService(
        store=NotificationStore(tmp_path / "notifications.sqlite"),
        policy=NotificationPolicy(minimum_cooldown_seconds=300, maximum_cooldown_seconds=300),
        clock=clock,
    )
    return clock, service


def _event(
    sequence: int,
    event_type: AnalysisEventType,
    task_id: str,
    occurred_at: datetime,
    *,
    deadline_at: datetime | None = None,
) -> AnalysisEvent:
    return AnalysisEvent(
        sequence=sequence,
        event_id=f"event-{sequence}",
        event_type=event_type,
        task_id=task_id,
        source="worker" if event_type.name.startswith("TASK_") else "orchestrator",
        occurred_at=occurred_at,
        deadline_at=deadline_at,
    )


def _now() -> datetime:
    return datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
