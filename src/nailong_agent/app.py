from __future__ import annotations

import argparse
import os
from pathlib import Path
from threading import Lock
from typing import Callable

from nailong_agent.event_bus import EventBus
from nailong_agent.events import EventEnvelope, PopupDecision
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore
from nailong_agent.renderer import NullRenderer, PopupRenderer, PySide6Renderer


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
    ) -> None:
        self.bus = bus or EventBus()
        self.lock = SingleInstanceLock(lock_path)
        self.renderer_factory = renderer_factory or PySide6Renderer
        self.privacy_store = privacy_store or PrivacyStore(lock_path.parent / "nailong_privacy.sqlite")
        self.privacy_policy = PrivacyPolicy(self.privacy_store.load_consent())
        self.renderer: PopupRenderer | None = None

    def run(self) -> int:
        if not self.lock.acquire():
            return 2
        try:
            self.renderer = self.renderer_factory()
            configure_privacy_controls = getattr(self.renderer, "configure_privacy_controls", None)
            if callable(configure_privacy_controls):
                configure_privacy_controls(on_clear_activity_history=self.privacy_store.clear_activity_history)
            if self.privacy_policy.needs_initial_consent:
                request_privacy_consent = getattr(self.renderer, "request_privacy_consent", None)
                consent = request_privacy_consent() if callable(request_privacy_consent) else None
                consent = consent or PrivacyConsent()
                self.privacy_store.save_consent(consent)
                self.privacy_policy = PrivacyPolicy(consent)
            self.bus.subscribe("PopupDecision", self._render_popup)
            self.bus.start()
            self.renderer.start()
            return self.renderer.exec()
        finally:
            self.bus.stop()
            if self.renderer is not None:
                self.renderer.stop()
            self.lock.release()

    def _render_popup(self, envelope: EventEnvelope) -> None:
        if self.renderer is None:
            return
        self.renderer.show(PopupDecision.model_validate(envelope.payload))


def create_renderer(*, headless: bool = False) -> PopupRenderer:
    if headless:
        return NullRenderer()
    return PySide6Renderer()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nailong desktop pet")
    parser.add_argument("--headless", action="store_true", help="run the process shell without PySide6")
    parser.add_argument("--lock-path", type=Path, default=Path(".runs/nailong-agent.lock"))
    args = parser.parse_args(argv)
    process = DesktopProcess(lock_path=args.lock_path, renderer_factory=lambda: create_renderer(headless=args.headless))
    return process.run()
