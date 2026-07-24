from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from nailong_agent.events import (
    NotificationIngestReceipt,
    NotificationIntent,
    NotificationStatus,
)
from nailong_agent.notification_policy import NotificationPolicy
from nailong_agent.notification_store import NotificationStore
from refactor_agent.analysis_events import AnalysisEvent


class NotificationPort(Protocol):
    def ingest_analysis_event(self, event: AnalysisEvent) -> NotificationIngestReceipt: ...

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
    ) -> None:
        self.store = store
        self.policy = policy or NotificationPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.minimum_popup_start_spacing_seconds = minimum_popup_start_spacing_seconds

    @classmethod
    def from_database(cls, database_path: Path) -> "NotificationService":
        return cls(store=NotificationStore(database_path))

    def ingest_analysis_event(self, event: AnalysisEvent) -> NotificationIngestReceipt:
        candidate = self.policy.candidate_for(event) if event.sensitivity == "public" else None
        return self.store.process_event(
            event,
            candidate,
            now=self.clock(),
            cooldown_seconds=self.policy.cooldown_seconds(),
        )

    def poll_long_tasks(self) -> NotificationIntent | None:
        return self.store.enqueue_due_long_task(
            self.policy.long_task_candidate(),
            now=self.clock(),
            cooldown_seconds=self.policy.cooldown_seconds(),
        )

    def set_do_not_disturb(self, enabled: bool) -> NotificationIntent | None:
        return self.store.set_do_not_disturb(
            enabled,
            now=self.clock(),
            summary_factory=self.policy.quiet_summary_candidate if not enabled else None,
        )

    def get_status(self) -> NotificationStatus:
        return self.store.status()

    def lease_next(self) -> NotificationIntent | None:
        return self.store.lease_next_intent(
            now=self.clock(),
            minimum_start_spacing_seconds=self.minimum_popup_start_spacing_seconds,
        )

    def acknowledge(self, notification_id: str, outcome: str) -> bool:
        return self.store.acknowledge(notification_id, outcome, now=self.clock())
