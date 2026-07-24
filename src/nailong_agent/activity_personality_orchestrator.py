from __future__ import annotations

from nailong_agent.activity_aggregator import ActivityEventAggregator
from nailong_agent.activity_recognizer import ActivityRecognizer
from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    RedactedActivitySignal,
)
from nailong_agent.event_bus import EventBus
from nailong_agent.events import ActivityEvent, ActivityWindow, EventEnvelope
from nailong_agent.notification_service import NotificationPort
from nailong_agent.personality_agent import PetPersonalityAgent


class ActivityPersonalityOrchestrator:
    """Connect minimized activity windows to personality and durable delivery."""

    def __init__(
        self,
        *,
        personality_agent: PetPersonalityAgent,
        notifications: NotificationPort,
        aggregator: ActivityEventAggregator | None = None,
        recognizer: ActivityRecognizer | None = None,
        aggregator_window_seconds: int = 60,
        personality_state_ttl_seconds: int = 300,
    ) -> None:
        if personality_state_ttl_seconds < 1:
            raise ValueError("personality state expiry must be positive")
        self.aggregator = aggregator or ActivityEventAggregator(
            window_seconds=aggregator_window_seconds
        )
        self.personality_agent = personality_agent
        self.notifications = notifications
        self.recognizer = recognizer or ActivityRecognizer()
        self.personality_state_ttl_seconds = personality_state_ttl_seconds
        self._recent_messages: list[str] = []

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe("ActivityEvent", self.on_activity_event)

    def on_activity_event(self, envelope: EventEnvelope) -> None:
        event = ActivityEvent.model_validate(envelope.payload)
        window = self.aggregator.ingest(event)
        if window is not None:
            self._handle_window(window)

    def flush(self) -> None:
        window = self.aggregator.flush()
        if window is not None:
            self._handle_window(window)

    def _handle_window(self, window: ActivityWindow) -> None:
        classification = self.recognizer.classify(window)
        state = self.personality_agent.run(
            PetDecisionInput(
                signal=RedactedActivitySignal(
                    event_id=_window_event_id(window),
                    occurred_at=window.window_ended_at,
                    source="system",
                    application_id=window.dominant_application,
                    redacted_summary=window.summary,
                ),
                classification=PetClassificationHint(
                    activity=classification.activity.value,
                    confidence=classification.confidence,
                    classifier=classification.classifier,
                ),
                context=PetDecisionContext(recent_messages=self._recent_messages),
            )
        )
        self.notifications.update_personality_state(
            emotion=state["emotion"],
            task_id=_window_event_id(window),
            expires_in_seconds=self.personality_state_ttl_seconds,
        )
        response = state["output"]
        if response is None:
            return
        self._recent_messages = [*self._recent_messages[-19:], response.message]
        self.notifications.ingest_personality_response(
            event_id=_window_event_id(window),
            occurred_at=window.window_ended_at,
            response=response,
        )


def _window_event_id(window: ActivityWindow) -> str:
    return f"activity-window:{int(window.window_started_at.timestamp())}"
