from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from threading import Event, Lock, Thread
from typing import Callable, Protocol

import httpx

from nailong_agent.notification_service import NotificationPort
from refactor_agent.analysis_events import AnalysisEvent


logger = logging.getLogger(__name__)


class AnalysisEventSource(Protocol):
    def iter_events(self, *, after: int, stop: Event) -> Iterable[AnalysisEvent]: ...


class HttpxSSEAnalysisEventSource:
    """Blocking SSE adapter with Last-Event-ID replay and bounded reconnect reads."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.stream_url = base_url.rstrip("/") + "/analysis/events/stream"
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5))
        )
        self._client_lock = Lock()
        self._active_client: httpx.Client | None = None

    def iter_events(self, *, after: int, stop: Event) -> Iterator[AnalysisEvent]:
        headers = {"Accept": "text/event-stream"}
        if after:
            headers["Last-Event-ID"] = str(after)
        client = self._client_factory()
        with self._client_lock:
            self._active_client = client
        try:
            with client:
                with client.stream(
                    "GET",
                    self.stream_url,
                    params={"after": after},
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    event_name = "message"
                    data_lines: list[str] = []
                    for line in response.iter_lines():
                        if stop.is_set():
                            return
                        if line == "":
                            if event_name == "analysis_event" and data_lines:
                                yield AnalysisEvent.model_validate(json.loads("\n".join(data_lines)))
                            event_name = "message"
                            data_lines.clear()
                        elif line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
        finally:
            with self._client_lock:
                if self._active_client is client:
                    self._active_client = None

    def close(self) -> None:
        with self._client_lock:
            client = self._active_client
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("SSE client was already closed", exc_info=True)


class AnalysisEventSubscriber:
    def __init__(
        self,
        *,
        source: AnalysisEventSource,
        notifications: NotificationPort,
        reconnect_seconds: float = 1.0,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.source = source
        self.notifications = notifications
        self.reconnect_seconds = reconnect_seconds
        self.on_error = on_error
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="analysis-event-subscriber", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 25.0) -> None:
        self._stop.set()
        close_source = getattr(self.source, "close", None)
        if callable(close_source):
            try:
                close_source()
            except Exception:
                logger.debug("Analysis event source close failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("analysis event subscriber did not stop before timeout")
            self._thread = None

    def consume_once(self) -> int:
        consumed = 0
        cursor = self.notifications.get_status().last_consumed_sequence
        for event in self.source.iter_events(after=cursor, stop=self._stop):
            if self._stop.is_set():
                break
            self.notifications.ingest_analysis_event(event)
            consumed += 1
        return consumed

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.consume_once()
            except Exception as exc:
                if self._stop.is_set():
                    return
                if self.on_error is not None:
                    try:
                        self.on_error(exc)
                    except Exception:
                        logger.exception("Analysis subscriber error callback failed")
                else:
                    logger.warning("Analysis event stream disconnected: %s", exc)
            self._stop.wait(self.reconnect_seconds)
