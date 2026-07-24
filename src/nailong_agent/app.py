from __future__ import annotations

import argparse
import os
from pathlib import Path
from threading import Lock
from typing import Callable

from nailong_agent.activity_collector import WindowActivityCollector
from nailong_agent.activity_personality_orchestrator import ActivityPersonalityOrchestrator
from nailong_agent.analysis_subscriber import AnalysisEventSubscriber, HttpxSSEAnalysisEventSource
from nailong_agent.config import NailongSettings
from nailong_agent.delivery import NotificationDeliveryPump
from nailong_agent.event_bus import EventBus
from nailong_agent.events import EventEnvelope, PopupDecision
from nailong_agent.notification_service import NotificationPort, NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.personality_agent import PetPersonalityAgent
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore
from nailong_agent.renderer import NullRenderer, PopupRenderer, PySide6Renderer
from nailong_agent.windows_activity import create_foreground_source, create_idle_source


class SingleInstanceLock:
    """Cross-process advisory lock backed by the platform file-lock primitive."""

    _process_locks: set[Path] = set()
    _guard = Lock()

    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._guard:
            if self.path in self._process_locks:
                return False
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(fd)
                return False
            self._fd = fd
            self._process_locks.add(self.path)
            self._acquired = True
            return True

    def release(self) -> None:
        if not self._acquired:
            return
        with self._guard:
            self._process_locks.discard(self.path)
            if self._fd is not None:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None
            self._acquired = False


class DesktopProcess:
    def __init__(
        self,
        *,
        lock_path: Path = Path(".runs/nailong-agent.lock"),
        bus: EventBus | None = None,
        renderer_factory: Callable[[], PopupRenderer] | None = None,
        privacy_store: PrivacyStore | None = None,
        privacy_policy: PrivacyPolicy | None = None,
        notification_service: NotificationPort | None = None,
        analysis_subscriber: AnalysisEventSubscriber | None = None,
        delivery_pump: NotificationDeliveryPump | None = None,
        activity_collector: WindowActivityCollector | None = None,
        activity_orchestrator: ActivityPersonalityOrchestrator | None = None,
    ) -> None:
        self.bus = bus or EventBus()
        self.lock = SingleInstanceLock(lock_path)
        self.renderer_factory = renderer_factory or PySide6Renderer
        self.privacy_store = privacy_store or PrivacyStore(lock_path.parent / "nailong_privacy.sqlite")
        self.privacy_policy = privacy_policy or PrivacyPolicy(self.privacy_store.load_consent())
        self.notification_service = notification_service
        self.analysis_subscriber = analysis_subscriber
        self.activity_collector = activity_collector
        self.activity_orchestrator = activity_orchestrator
        self.delivery_pump = delivery_pump or (
            NotificationDeliveryPump(notifications=notification_service, bus=self.bus)
            if notification_service is not None
            else None
        )
        self.renderer: PopupRenderer | None = None

    def run(self) -> int:
        if not self.lock.acquire():
            return 2
        try:
            self.renderer = self.renderer_factory()
            configure_privacy_controls = getattr(self.renderer, "configure_privacy_controls", None)
            if callable(configure_privacy_controls):
                configure_privacy_controls(on_clear_activity_history=self.privacy_store.clear_activity_history)
            configure_notification_controls = getattr(self.renderer, "configure_notification_controls", None)
            if callable(configure_notification_controls) and self.notification_service is not None:
                configure_notification_controls(
                    on_set_do_not_disturb=lambda enabled: self.notification_service.set_do_not_disturb(enabled),
                    get_do_not_disturb=lambda: self.notification_service.get_status().do_not_disturb,
                )
            if self.privacy_policy.needs_initial_consent:
                request_privacy_consent = getattr(self.renderer, "request_privacy_consent", None)
                consent = request_privacy_consent() if callable(request_privacy_consent) else None
                consent = consent or PrivacyConsent()
                self.privacy_store.save_consent(consent)
                self.privacy_policy.consent = consent
            self.bus.subscribe("PopupDecision", self._render_popup)
            if self.activity_orchestrator is not None:
                self.activity_orchestrator.subscribe(self.bus)
            self.bus.start()
            if self.activity_collector is not None:
                self.activity_collector.start()
            self.renderer.start()
            if self.analysis_subscriber is not None:
                self.analysis_subscriber.start()
            if self.delivery_pump is not None:
                self.delivery_pump.start()
            return self.renderer.exec()
        finally:
            if self.activity_collector is not None:
                self.activity_collector.stop()
            self.bus.wait_idle(2.0)
            if self.delivery_pump is not None:
                self.delivery_pump.stop()
            if self.analysis_subscriber is not None:
                self.analysis_subscriber.stop()
            self.bus.stop()
            if self.renderer is not None:
                self.renderer.stop()
            self.lock.release()

    def _render_popup(self, envelope: EventEnvelope) -> None:
        if self.renderer is None:
            return
        decision = PopupDecision.model_validate(envelope.payload)
        notification_id = decision.dedupe_key
        if (
            self.notification_service is not None
            and notification_id
            and self.notification_service.get_status().do_not_disturb
        ):
            self.notification_service.acknowledge(notification_id, "dismissed")
            return
        try:
            accepted = self.renderer.show(decision)
        except Exception:
            if self.notification_service is not None and notification_id:
                self.notification_service.acknowledge(notification_id, "failed")
            raise
        if self.notification_service is not None and notification_id:
            self.notification_service.acknowledge(
                notification_id,
                "dismissed" if accepted is False else "shown",
            )


def create_renderer(*, headless: bool = False) -> PopupRenderer:
    if headless:
        return NullRenderer()
    return PySide6Renderer()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nailong desktop pet")
    parser.add_argument("--headless", action="store_true", help="run the process shell without PySide6")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--lock-path", type=Path)
    parser.add_argument("--privacy-database", type=Path)
    parser.add_argument("--analysis-url", help="subscribe to the Refactor Agent analysis SSE stream")
    parser.add_argument("--notification-database", type=Path)
    parser.add_argument("--maximum-popups-per-day", type=int)
    parser.add_argument("--minimum-cooldown-seconds", type=int)
    parser.add_argument("--maximum-cooldown-seconds", type=int)
    parser.add_argument("--activity-listener", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args(argv)
    settings = NailongSettings.from_env().with_overrides(
        data_dir=args.data_dir,
        lock_path=args.lock_path,
        privacy_database=args.privacy_database,
        notification_database=args.notification_database,
        analysis_url=args.analysis_url,
        maximum_popups_per_day=args.maximum_popups_per_day,
        minimum_cooldown_seconds=args.minimum_cooldown_seconds,
        maximum_cooldown_seconds=args.maximum_cooldown_seconds,
        activity_listener_enabled=args.activity_listener,
    )
    privacy_store = PrivacyStore(settings.privacy_database)
    notification_store = (
        NotificationStore(settings.notification_database)
        if settings.analysis_url or settings.activity_listener_enabled
        else None
    )
    notifications = (
        NotificationService(
            store=notification_store,
            preference_overrides=settings.notification_preference_overrides,
        )
        if notification_store is not None
        else None
    )
    subscriber = (
        AnalysisEventSubscriber(
            source=HttpxSSEAnalysisEventSource(settings.analysis_url),
            notifications=notifications,
        )
        if settings.analysis_url and notifications is not None
        else None
    )
    privacy_policy = PrivacyPolicy(privacy_store.load_consent())
    bus = EventBus()
    collector = (
        WindowActivityCollector(
            source=create_foreground_source(),
            idle_source=create_idle_source(),
            privacy_policy=privacy_policy,
            privacy_store=privacy_store,
            event_bus=bus,
            preferences=notification_store.get_preferences,
            application_rules=notification_store.list_application_rules,
        )
        if settings.activity_listener_enabled and notification_store is not None
        else None
    )
    activity_orchestrator = (
        ActivityPersonalityOrchestrator(
            personality_agent=PetPersonalityAgent(
                intensity=notification_store.get_preferences().personality_intensity.lower(),
            ),
            notifications=notifications,
        )
        if settings.activity_listener_enabled and notifications is not None and notification_store is not None
        else None
    )
    process = DesktopProcess(
        lock_path=settings.lock_path,
        bus=bus,
        renderer_factory=lambda: create_renderer(headless=args.headless),
        privacy_store=privacy_store,
        privacy_policy=privacy_policy,
        notification_service=notifications,
        analysis_subscriber=subscriber,
        activity_collector=collector,
        activity_orchestrator=activity_orchestrator,
    )
    return process.run()
