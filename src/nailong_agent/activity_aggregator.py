from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from nailong_agent.events import ActivityEvent, ActivityType, ActivityWindow


@dataclass
class _WindowState:
    started_at: datetime
    ended_at: datetime
    applications: Counter[str] = field(default_factory=Counter)
    activities: Counter[ActivityType] = field(default_factory=Counter)
    confidence_total: float = 0.0
    event_count: int = 0
    duplicate_count: int = 0
    summary: str | None = None


class ActivityEventAggregator:
    """Deduplicate minimized signals and emit deterministic tumbling windows."""

    def __init__(self, *, dedupe_seconds: int = 5, window_seconds: int = 60) -> None:
        if dedupe_seconds < 0:
            raise ValueError("dedupe_seconds must be nonnegative")
        if window_seconds < 1:
            raise ValueError("window_seconds must be positive")
        self.dedupe_interval = timedelta(seconds=dedupe_seconds)
        self.window_seconds = window_seconds
        self._state: _WindowState | None = None
        self._last_fingerprints: dict[str, datetime] = {}

    def ingest(self, event: ActivityEvent) -> ActivityWindow | None:
        if event.sensitivity != "public":
            raise ValueError("only public minimized activity events may be aggregated")
        if self._state is not None and event.occurred_at < self._state.started_at:
            raise ValueError("activity events must not precede the active window")

        fingerprint = _fingerprint(event)
        previous = self._last_fingerprints.get(fingerprint)
        if previous is not None and event.occurred_at - previous <= self.dedupe_interval:
            if event.occurred_at < previous:
                raise ValueError("activity events must be ordered")
            self._last_fingerprints[fingerprint] = event.occurred_at
            if self._state is not None:
                self._state.duplicate_count += 1
            return None

        self._last_fingerprints[fingerprint] = event.occurred_at
        self._prune_fingerprints(event.occurred_at)
        window_start = _window_start(event.occurred_at, self.window_seconds)
        emitted = None
        if self._state is None:
            self._state = self._new_state(window_start)
        elif window_start > self._state.started_at:
            emitted = self._finalize()
            self._state = self._new_state(window_start)
        self._add(event)
        return emitted

    def flush(self) -> ActivityWindow | None:
        if self._state is None or self._state.event_count == 0:
            self._state = None
            return None
        emitted = self._finalize()
        self._state = None
        return emitted

    def _new_state(self, started_at: datetime) -> _WindowState:
        return _WindowState(
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=self.window_seconds),
        )

    def _add(self, event: ActivityEvent) -> None:
        assert self._state is not None
        self._state.applications[event.application_id] += 1
        self._state.activities[event.activity] += 1
        self._state.confidence_total += event.confidence
        self._state.event_count += 1
        if self._state.summary is None and event.summary:
            self._state.summary = event.summary

    def _finalize(self) -> ActivityWindow:
        assert self._state is not None and self._state.event_count > 0
        state = self._state
        return ActivityWindow(
            window_started_at=state.started_at,
            window_ended_at=state.ended_at,
            dominant_application=max(state.applications, key=state.applications.get),
            dominant_activity=max(state.activities, key=state.activities.get),
            confidence=state.confidence_total / state.event_count,
            event_count=state.event_count,
            duplicate_count=state.duplicate_count,
            summary=state.summary,
        )

    def _prune_fingerprints(self, now: datetime) -> None:
        cutoff = now - self.dedupe_interval
        self._last_fingerprints = {
            key: seen_at for key, seen_at in self._last_fingerprints.items() if seen_at >= cutoff
        }


def _fingerprint(event: ActivityEvent) -> str:
    summary = " ".join((event.summary or "").casefold().split())
    return "|".join((event.source, event.application_id, event.activity.value, summary))


def _window_start(value: datetime, window_seconds: int) -> datetime:
    timestamp = int(value.timestamp())
    start = timestamp - timestamp % window_seconds
    return datetime.fromtimestamp(start, timezone.utc)
