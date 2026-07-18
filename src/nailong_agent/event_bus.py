from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from queue import Full, Queue
from threading import Event, Lock, Thread
from typing import Any

from nailong_agent.events import EventEnvelope

EventHandler = Callable[[EventEnvelope], Any]


class EventBusError(RuntimeError):
    pass


class EventBus:
    """A bounded, stoppable, single-worker event bus for the desktop process."""

    def __init__(self, *, max_queue_size: int = 100, on_error: Callable[[Exception, EventEnvelope], None] | None = None) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        self._queue: Queue[EventEnvelope | None] = Queue(maxsize=max_queue_size)
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._lock = Lock()
        self._stopped = Event()
        self._thread: Thread | None = None
        self._on_error = on_error
        self.dropped_count = 0
        self._closed = False

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        with self._lock:
            self._handlers[event_type].append(handler)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._closed = False
            self._stopped.clear()
            self._thread = Thread(target=self._run, name="nailong-event-bus", daemon=True)
            self._thread.start()

    def publish(self, event: EventEnvelope) -> bool:
        if self._closed:
            raise EventBusError("event bus is stopped")
        try:
            self._queue.put_nowait(event)
        except Full:
            self.dropped_count += 1
            return False
        return True

    def wait_idle(self, timeout: float | None = None) -> bool:
        if timeout is None:
            self._queue.join()
            return True
        finished = Event()

        def wait_for_queue() -> None:
            self._queue.join()
            finished.set()

        waiter = Thread(target=wait_for_queue, name="nailong-event-bus-waiter", daemon=True)
        waiter.start()
        return finished.wait(timeout)

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            self._stopped.set()
            self._closed = True
            return
        self._stopped.set()
        self._queue.put(None)
        thread.join(timeout)
        if thread.is_alive():
            raise EventBusError("event bus did not stop before timeout")
        self._thread = None
        self._closed = True

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                with self._lock:
                    handlers = [*self._handlers.get(event.event_type, []), *self._handlers.get("*", [])]
                for handler in handlers:
                    try:
                        handler(event)
                    except Exception as exc:  # one subscriber must not kill the bus
                        if self._on_error is not None:
                            try:
                                self._on_error(exc, event)
                            except Exception:
                                pass
            finally:
                self._queue.task_done()
