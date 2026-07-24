from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nailong_agent.activity_aggregator import ActivityEventAggregator
from nailong_agent.events import ActivityEvent, ActivityType


START = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)


def _event(
    seconds: int,
    *,
    application: str = "code",
    activity: ActivityType = ActivityType.CODING,
    confidence: float = 0.8,
    summary: str | None = "application=code; activity=coding; source=window",
    sensitivity: str = "public",
) -> ActivityEvent:
    return ActivityEvent(
        occurred_at=START + timedelta(seconds=seconds),
        source="window",
        application_id=application,
        activity=activity,
        confidence=confidence,
        summary=summary,
        sensitivity=sensitivity,
    )


def test_same_semantic_event_inside_five_seconds_is_deduplicated() -> None:
    aggregator = ActivityEventAggregator()

    assert aggregator.ingest(_event(1)) is None
    assert aggregator.ingest(_event(5)) is None
    window = aggregator.flush()

    assert window is not None
    assert window.event_count == 1
    assert window.duplicate_count == 1


def test_different_application_or_activity_is_not_deduplicated() -> None:
    aggregator = ActivityEventAggregator()

    aggregator.ingest(_event(1))
    aggregator.ingest(
        _event(
            2,
            application="browser",
            activity=ActivityType.READING,
            confidence=0.6,
            summary="application=browser; activity=reading; source=window",
        )
    )
    aggregator.ingest(
        _event(
            3,
            activity=ActivityType.DEBUGGING,
            confidence=1.0,
            summary="application=code; activity=debugging; source=window",
        )
    )
    window = aggregator.flush()

    assert window is not None
    assert window.event_count == 3
    assert window.duplicate_count == 0


def test_window_boundary_emits_deterministic_aggregate() -> None:
    aggregator = ActivityEventAggregator()
    aggregator.ingest(_event(1, confidence=0.6))
    aggregator.ingest(_event(10, activity=ActivityType.DEBUGGING, confidence=0.9))
    aggregator.ingest(_event(20, confidence=0.9))

    first = aggregator.ingest(
        _event(
            60,
            application="browser",
            activity=ActivityType.READING,
            confidence=0.7,
            summary="application=browser; activity=reading; source=window",
        )
    )

    assert first is not None
    assert first.window_started_at == START
    assert first.window_ended_at == START + timedelta(seconds=60)
    assert first.dominant_application == "code"
    assert first.dominant_activity == ActivityType.CODING
    assert first.confidence == pytest.approx(0.8)
    assert first.event_count == 3
    assert first.duplicate_count == 0
    assert first.summary == "application=code; activity=coding; source=window"

    second = aggregator.flush()
    assert second is not None
    assert second.window_started_at == START + timedelta(seconds=60)
    assert second.dominant_application == "browser"
    assert second.event_count == 1
    assert aggregator.flush() is None


def test_rejects_out_of_order_and_private_events() -> None:
    aggregator = ActivityEventAggregator()
    aggregator.ingest(_event(65))

    with pytest.raises(ValueError, match="precede"):
        aggregator.ingest(_event(59, activity=ActivityType.DEBUGGING))
    with pytest.raises(ValueError, match="public"):
        aggregator.ingest(_event(66, sensitivity="private"))
