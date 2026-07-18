from __future__ import annotations

import threading
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from refactor_agent.config import AppSettings
from refactor_agent.execution_control import ExecutionCancelled, ExecutionControl, ExecutionDeadlineExceeded
from refactor_agent.github import GitHubAutomationService
from refactor_agent.local_repository import LocalRepositoryRefactorService
from refactor_agent.models import GitHubRefactorJob, RepositoryJobKind
from refactor_agent.repository_allowlist import RepositoryAllowlistPolicy
from refactor_agent.store import JobTransitionError, SQLiteRunStore


logger = logging.getLogger(__name__)


class GitHubJobWorker:
    def __init__(
        self,
        settings: AppSettings,
        service: GitHubAutomationService,
        store: SQLiteRunStore,
        poll_seconds: float = 1.0,
        local_service: LocalRepositoryRefactorService | None = None,
        repository_policy: RepositoryAllowlistPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.store = store
        self.repository_policy = repository_policy or RepositoryAllowlistPolicy(settings, store)
        self.local_service = local_service or LocalRepositoryRefactorService(
            settings,
            repository_policy=self.repository_policy,
        )
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
            self.settings.job_deadline_seconds,
        )
        if record is None:
            return False
        if not record.payload_json:
            self.store.mark_github_job_failed(
                record.job_id,
                "durable job payload is missing",
                worker_id=self.worker_id,
            )
            return True
        try:
            job = GitHubRefactorJob.model_validate_json(record.payload_json)
        except ValueError as exc:
            self.store.mark_github_job_failed(
                record.job_id,
                f"invalid durable job payload: {exc}",
                worker_id=self.worker_id,
            )
            return True
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        control = ExecutionControl(
            deadline_at=(
                datetime.fromisoformat(record.deadline_at)
                if record.deadline_at
                else datetime.now(timezone.utc) + timedelta(seconds=self.settings.job_deadline_seconds)
            ),
            is_cancel_requested=lambda: lease_lost.is_set() or self._cancel_requested(record.job_id),
        )
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(record.job_id, heartbeat_stop, lease_lost),
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            self.repository_policy.require_allowed(job.repo_full_name)
            processor = (
                self.local_service
                if job.job_kind == RepositoryJobKind.DASHBOARD_URL
                else self.service
            )
            result = processor.process(job, execution_control=control)
            if not result.requires_manual_cleanup:
                control.checkpoint("after-github-service")
        except ExecutionCancelled:
            try:
                self.store.transition_github_job(
                    job.job_id,
                    "CANCELLED",
                    worker_id=self.worker_id,
                    require_owner=True,
                    message="worker stopped after cancellation request",
                )
            except JobTransitionError:
                logger.info("Worker %s no longer owns cancelled job %s", self.worker_id, job.job_id)
        except ExecutionDeadlineExceeded as exc:
            try:
                self.store.mark_github_job_timed_out(job.job_id, str(exc), self.worker_id)
            except JobTransitionError:
                logger.info("Worker %s no longer owns timed out job %s", self.worker_id, job.job_id)
        except Exception as exc:
            try:
                self.store.fail_github_job(job, str(exc), worker_id=self.worker_id)
            except JobTransitionError:
                logger.info("Worker %s no longer owns failed job %s", self.worker_id, job.job_id)
        else:
            if not self.settings.retain_checkouts:
                result.workspace_path = None
            try:
                self.store.complete_github_job(job, result, worker_id=self.worker_id)
            except JobTransitionError:
                logger.info("Worker %s no longer owns completed job %s", self.worker_id, job.job_id)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
        return True

    def _heartbeat(self, job_id: str, stop: threading.Event, lease_lost: threading.Event) -> None:
        interval = max(self.settings.job_lease_seconds / 3, 1)
        while not stop.wait(interval):
            if not self.store.renew_github_job_lease(
                job_id,
                self.worker_id,
                self.settings.job_lease_seconds,
            ):
                lease_lost.set()
                return

    def _cancel_requested(self, job_id: str) -> bool:
        record = self.store.get_github_job(job_id)
        return record is None or record.status == "CANCEL_REQUESTED"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("GitHub job worker iteration failed")
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)
