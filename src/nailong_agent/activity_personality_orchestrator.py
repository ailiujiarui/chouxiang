from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread, current_thread

from nailong_agent.activity_aggregator import ActivityEventAggregator
from nailong_agent.activity_recognizer import ActivityRecognizer
from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    RedactedActivitySignal,
)
from nailong_agent.event_bus import EventBus
from nailong_agent.events import ActivityEvent, ActivityType, ActivityWindow, EventEnvelope
from nailong_agent.health import NailongHealthMonitor
from nailong_agent.notification_service import NotificationPort
from nailong_agent.personality_agent import PetPersonalityAgent


logger = logging.getLogger(__name__)

_TRANSIENT_GAME_INTERRUPTION_APPLICATIONS = {"other", "explorer"}


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
        scheduler_interval_seconds: float = 5.0,
        game_interruption_grace_seconds: int = 15,
        clock: Callable[[], datetime] | None = None,
        health_monitor: NailongHealthMonitor | None = None,
    ) -> None:
        if personality_state_ttl_seconds < 1:
            raise ValueError("personality state expiry must be positive")
        if scheduler_interval_seconds <= 0:
            raise ValueError("scheduler interval must be positive")
        if game_interruption_grace_seconds < 0:
            raise ValueError("game interruption grace must be nonnegative")
        self.aggregator = aggregator or ActivityEventAggregator(
            window_seconds=aggregator_window_seconds
        )
        self.personality_agent = personality_agent
        self.notifications = notifications
        self.recognizer = recognizer or ActivityRecognizer()
        self.personality_state_ttl_seconds = personality_state_ttl_seconds
        self.scheduler_interval_seconds = scheduler_interval_seconds
        self.game_interruption_grace = timedelta(seconds=game_interruption_grace_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.health_monitor = health_monitor
        self._recent_messages: list[str] = []
        self._aggregator_lock = Lock()
        self._activity_state_lock = Lock()
        self._processing_lock = Lock()
        self._stopped = Event()
        self._thread: Thread | None = None
        self._gaming_started_at: datetime | None = None
        self._gaming_interrupted_at: datetime | None = None
        self._gaming_tease_issued = False

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe("ActivityEvent", self.on_activity_event)

    def start(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                return
            self._thread = None
        self._stopped.clear()
        self._thread = Thread(
            target=self._run_scheduler,
            name="nailong-activity-window-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(2.0)
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None

    def on_activity_event(self, envelope: EventEnvelope) -> None:
        event = ActivityEvent.model_validate(envelope.payload)
        with self._activity_state_lock:
            self._expire_game_session_locked(event.occurred_at)
            if event.application_id == "game" or event.activity is ActivityType.GAMING:
                if self._gaming_started_at is None:
                    self._gaming_started_at = event.occurred_at
                self._gaming_interrupted_at = None
            elif (
                self._gaming_started_at is not None
                and event.application_id not in _TRANSIENT_GAME_INTERRUPTION_APPLICATIONS
                and self._gaming_interrupted_at is None
            ):
                self._gaming_interrupted_at = event.occurred_at
        with self._aggregator_lock:
            window = self.aggregator.ingest(event)
        if window is not None:
            self._handle_window(window)

    def flush(self) -> None:
        with self._aggregator_lock:
            window = self.aggregator.flush()
        if window is not None:
            self._handle_window(window)

    def flush_due(self, now: datetime | None = None) -> bool:
        current_time = now or self.clock()
        with self._activity_state_lock:
            self._expire_game_session_locked(current_time)
        with self._aggregator_lock:
            window = self.aggregator.flush_due(current_time)
        if window is None:
            return False
        self._handle_window(window)
        return True

    def _run_scheduler(self) -> None:
        while not self._stopped.wait(self.scheduler_interval_seconds):
            try:
                self.flush_due()
                self.process_due_game_session(self.clock())
            except Exception:
                # A transient classifier or storage failure must not disable future windows.
                logger.exception("activity window processing failed")
                continue

    def process_due_game_session(self, now: datetime | None = None) -> bool:
        current_time = now or self.clock()
        with self._activity_state_lock:
            self._expire_game_session_locked(current_time)
            gaming_started_at = self._gaming_started_at
            if (
                gaming_started_at is None
                or self._gaming_interrupted_at is not None
                or self._gaming_tease_issued
                or current_time - gaming_started_at < timedelta(minutes=5)
            ):
                return False
        self._handle_window(
            ActivityWindow(
                window_started_at=gaming_started_at,
                window_ended_at=current_time,
                dominant_application="game",
                dominant_activity=ActivityType.GAMING,
                confidence=0.95,
                event_count=1,
                summary="application=game; activity=gaming; source=window",
            ),
            event_id=f"game-session:{int(gaming_started_at.timestamp())}",
        )
        with self._activity_state_lock:
            return self._gaming_tease_issued

    def _expire_game_session_locked(self, now: datetime) -> None:
        if (
            self._gaming_interrupted_at is not None
            and now - self._gaming_interrupted_at >= self.game_interruption_grace
        ):
            self._gaming_started_at = None
            self._gaming_interrupted_at = None
            self._gaming_tease_issued = False

    def _handle_window(self, window: ActivityWindow, *, event_id: str | None = None) -> None:
        with self._processing_lock:
            activity_event_id = event_id or _window_event_id(window)
            classification = self.recognizer.classify(window)
            if self.health_monitor is not None:
                self.health_monitor.record_classification(
                    application_category=window.dominant_application,
                    activity_type=classification.activity.value,
                    confidence=classification.confidence,
                )
            game_tease_enabled = False
            with self._activity_state_lock:
                gaming_started_at = self._gaming_started_at
                gaming_interrupted_at = self._gaming_interrupted_at
                gaming_tease_issued = self._gaming_tease_issued
            if (
                classification.activity is ActivityType.GAMING
                and gaming_started_at is not None
                and gaming_interrupted_at is None
            ):
                get_preferences = getattr(self.notifications, "get_preferences", None)
                preferences = get_preferences() if callable(get_preferences) else None
                game_tease_enabled = bool(
                    preferences is not None
                    and preferences.game_tease_enabled
                    and not gaming_tease_issued
                    and window.window_ended_at - gaming_started_at >= timedelta(minutes=5)
                )
            state = self.personality_agent.run(
                PetDecisionInput(
                    signal=RedactedActivitySignal(
                        event_id=activity_event_id,
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
                    context=PetDecisionContext(
                        recent_messages=self._recent_messages,
                        game_tease_enabled=game_tease_enabled,
                    ),
                )
            )
            self.notifications.update_personality_state(
                emotion=state["emotion"],
                task_id=activity_event_id,
                expires_in_seconds=self.personality_state_ttl_seconds,
            )
            response = state["output"]
            if response is None:
                return
            self._recent_messages = [*self._recent_messages[-19:], response.message]
            self.notifications.ingest_personality_response(
                event_id=activity_event_id,
                occurred_at=window.window_ended_at,
                response=response,
            )
            if response.intent == "tease":
                with self._activity_state_lock:
                    self._gaming_tease_issued = True


def _window_event_id(window: ActivityWindow) -> str:
    return f"activity-window:{int(window.window_started_at.timestamp())}"
