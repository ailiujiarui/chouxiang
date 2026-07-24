from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from nailong_agent.events import (
    NotificationIngestReceipt,
    NotificationIntent,
    NotificationKind,
    NotificationStatus,
)
from nailong_agent.contracts import PetPersonalityResponse
from nailong_agent.notification_policy import NotificationCandidate, NotificationPolicy
from nailong_agent.notification_store import NotificationStore
from refactor_agent.analysis_events import AnalysisEvent


class NotificationPort(Protocol):
    def ingest_analysis_event(self, event: AnalysisEvent) -> NotificationIngestReceipt: ...

    def ingest_personality_response(
        self,
        *,
        event_id: str,
        occurred_at: datetime,
        response: PetPersonalityResponse,
    ) -> NotificationIngestReceipt: ...

    def poll_long_tasks(self) -> NotificationIntent | None: ...

    def set_do_not_disturb(self, enabled: bool) -> NotificationIntent | None: ...

    def get_status(self) -> NotificationStatus: ...

    def lease_next(self) -> NotificationIntent | None: ...

    def acknowledge(self, notification_id: str, outcome: str) -> bool: ...


class NotificationService:
    """Callable desktop boundary used by the SSE subscriber, renderer, and other modules."""

    def __init__(
        self,
        *,
        store: NotificationStore,
        policy: NotificationPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        minimum_popup_start_spacing_seconds: int = 30,
        preference_overrides: dict[str, int] | None = None,
    ) -> None:
        self.store = store
        self._policy_is_explicit = policy is not None
        self.policy = policy or NotificationPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.minimum_popup_start_spacing_seconds = minimum_popup_start_spacing_seconds
        self.preference_overrides = preference_overrides or {}

    @classmethod
    def from_database(
        cls,
        database_path: Path,
        *,
        preference_overrides: dict[str, int] | None = None,
    ) -> "NotificationService":
        return cls(store=NotificationStore(database_path), preference_overrides=preference_overrides)

    def ingest_analysis_event(self, event: AnalysisEvent) -> NotificationIngestReceipt:
        preferences = self._effective_preferences()
        candidate = self.policy.candidate_for(event) if event.sensitivity == "public" else None
        return self.store.process_event(
            event,
            candidate,
            now=self.clock(),
            cooldown_seconds=self._cooldown_seconds(preferences),
            preferences=preferences,
        )

    def poll_long_tasks(self) -> NotificationIntent | None:
        preferences = self._effective_preferences()
        return self.store.enqueue_due_long_task(
            self.policy.long_task_candidate(),
            now=self.clock(),
            cooldown_seconds=self._cooldown_seconds(preferences),
        )

    def set_do_not_disturb(self, enabled: bool) -> NotificationIntent | None:
        return self.store.set_do_not_disturb(
            enabled,
            now=self.clock(),
            summary_factory=self.policy.quiet_summary_candidate if not enabled else None,
        )

    def get_status(self) -> NotificationStatus:
        return self.store.status(now=self.clock())

    def lease_next(self) -> NotificationIntent | None:
        return self.store.lease_next_intent(
            now=self.clock(),
            minimum_start_spacing_seconds=self.minimum_popup_start_spacing_seconds,
            preferences=self._effective_preferences(),
        )

    def ingest_personality_response(
        self,
        *,
        event_id: str,
        occurred_at: datetime,
        response: PetPersonalityResponse,
    ) -> NotificationIngestReceipt:
        candidate = _candidate_for_personality_response(response)
        return self.store.process_personality_event(
            event_id=event_id,
            occurred_at=occurred_at,
            candidate=candidate,
            now=self.clock(),
            cooldown_seconds=self._cooldown_seconds(self._effective_preferences()),
            preferences=self._effective_preferences(),
        )

    def acknowledge(self, notification_id: str, outcome: str) -> bool:
        return self.store.acknowledge(notification_id, outcome, now=self.clock())

    def _effective_preferences(self):
        return self.store.get_preferences().model_copy(update=self.preference_overrides)

    def _cooldown_seconds(self, preferences):
        if self._policy_is_explicit and not self.preference_overrides:
            return self.policy.cooldown_seconds()
        return self.policy.cooldown_seconds(
            minimum=preferences.minimum_cooldown_seconds,
            maximum=preferences.maximum_cooldown_seconds,
        )


def _candidate_for_personality_response(response: PetPersonalityResponse):
    kinds = {
        "encourage": NotificationKind.ENCOURAGEMENT,
        "remind": NotificationKind.DEBUG_HINT,
        "celebrate": NotificationKind.PYTEST_CELEBRATION,
        "ask": NotificationKind.LIGHT_TEASE,
    }
    return NotificationCandidate(kinds[response.intent], response.message)
