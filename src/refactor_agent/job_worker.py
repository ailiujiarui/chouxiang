from __future__ import annotations

import threading
import logging
from uuid import uuid4

from refactor_agent.config import AppSettings
from refactor_agent.github import GitHubAutomationService
from refactor_agent.models import GitHubRefactorJob
from refactor_agent.store import SQLiteRunStore


logger = logging.getLogger(__name__)


class GitHubJobWorker:
    def __init__(
        self,
        settings: AppSettings,
        service: GitHubAutomationService,
        store: SQLiteRunStore,
        poll_seconds: float = 1.0,
    ) -> None:
        self.settings = settings
        self.service = service
        self.store = store
        self.poll_seconds = poll_seconds
        self.worker_id = f"worker-{uuid4().hex}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.worker_id, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def run_once(self) -> bool:
        record = self.store.claim_next_github_job(
            self.worker_id,
            self.settings.job_lease_seconds,
            self.settings.job_max_attempts,
        )
        if record is None:
            return False
        if not record.payload_json:
            self.store.mark_github_job_failed(record.job_id, "durable job payload is missing")
            return True
        try:
            job = GitHubRefactorJob.model_validate_json(record.payload_json)
        except ValueError as exc:
            self.store.mark_github_job_failed(record.job_id, f"invalid durable job payload: {exc}")
            return True
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(record.job_id, heartbeat_stop),
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self.service.process(job)
        except Exception as exc:
            self.store.fail_github_job(job, str(exc))
        else:
            if not self.settings.retain_checkouts:
                result.workspace_path = None
            self.store.complete_github_job(job, result)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
        return True

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        interval = max(self.settings.job_lease_seconds / 3, 1)
        while not stop.wait(interval):
            if not self.store.renew_github_job_lease(
                job_id,
                self.worker_id,
                self.settings.job_lease_seconds,
            ):
                return

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("GitHub job worker iteration failed")
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)
