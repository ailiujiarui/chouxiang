from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread

from nailong_agent.event_bus import EventBus, EventBusError
from nailong_agent.events import NotificationIntent, PopupDecision
from nailong_agent.health import NailongHealthMonitor
from nailong_agent.notification_service import NotificationPort


class NotificationDeliveryPump:
    """Moves durable notification intents onto the in-process UI event bus."""

    def __init__(
        self,
        *,
        notifications: NotificationPort,
        bus: EventBus,
        poll_seconds: float = 1.0,
        presentation_gate: Callable[[], bool] | None = None,
        health_monitor: NailongHealthMonitor | None = None,
    ) -> None:
        self.notifications = notifications
        self.bus = bus
        self.poll_seconds = poll_seconds
        self.presentation_gate = presentation_gate
        self.health_monitor = health_monitor
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="notification-delivery-pump", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("notification delivery pump did not stop before timeout")
            self._thread = None

    def run_once(self) -> bool:
        self.notifications.poll_long_tasks()
        game_tease_only = False
        if self.presentation_gate is not None and not self.presentation_gate():
            if self.health_monitor is not None:
                self.health_monitor.record_delivery(fullscreen_blocked=True)
            if not self.notifications.get_preferences().game_tease_enabled:
                return False
            game_tease_only = True
        elif self.health_monitor is not None:
            self.health_monitor.record_delivery(fullscreen_blocked=False)
        intent = self.notifications.lease_next(game_tease_only=game_tease_only)
        if intent is None:
            return False
        try:
            published = self.bus.publish(_popup_decision(intent).envelope())
        except EventBusError:
            published = False
        if not published:
            self.notifications.acknowledge(intent.notification_id, "failed")
            if self.health_monitor is not None:
                self.health_monitor.record_delivery(
                    outcome="failed",
                    error_code="delivery_publish_failed",
                )
        elif self.health_monitor is not None:
            self.health_monitor.record_delivery(outcome="queued_for_render")
        return published

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.poll_seconds)


def _popup_decision(intent: NotificationIntent) -> PopupDecision:
    return PopupDecision(
        action="show",
        reason=f"proactive:{intent.kind.value}",
        message=intent.message,
        priority=intent.priority,
        display_seconds=15 if intent.priority == "high" else 12,
        dedupe_key=intent.notification_id,
    )
