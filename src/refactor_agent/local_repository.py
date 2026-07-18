from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from refactor_agent.config import AppSettings
from refactor_agent.execution_control import (
    ExecutionCancelled,
    ExecutionControl,
    ExecutionDeadlineExceeded,
)
from refactor_agent.github import GitRepositoryManager, _repo_relative_path
from refactor_agent.llm import DeepSeekClient, MockRefactorClient, RefactorClient
from refactor_agent.locator import AUTO_TARGET_PATH, locate_source_file
from refactor_agent.models import (
    GitHubAutomationResult,
    GitHubRefactorJob,
    RefactorRequest,
    RepositoryJobKind,
)
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.repository_allowlist import RepositoryAllowlistPolicy
from refactor_agent.store import SQLiteRunStore


class ReadOnlyRepositoryManager(Protocol):
    def clone_repository(
        self,
        repo_full_name: str,
        ref: str | None,
        token: str | None,
        checkout_label: str,
    ) -> Path: ...

    def cleanup(self, checkout_path: Path) -> None: ...


LLMFactory = Callable[[], RefactorClient]


class LocalRepositoryRefactorService:
    def __init__(
        self,
        settings: AppSettings,
        repo_manager: ReadOnlyRepositoryManager | None = None,
        llm_factory: LLMFactory | None = None,
        repository_policy: RepositoryAllowlistPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.repo_manager = repo_manager
        self.llm_factory = llm_factory or self._default_llm_factory
        self.repository_policy = repository_policy or RepositoryAllowlistPolicy(
            settings,
            SQLiteRunStore(settings.resolved_database_path),
        )

    def process(
        self,
        job: GitHubRefactorJob,
        execution_control: ExecutionControl | None = None,
    ) -> GitHubAutomationResult:
        if job.job_kind != RepositoryJobKind.DASHBOARD_URL:
            raise ValueError("Local repository service only accepts DASHBOARD_URL jobs.")
        control = execution_control or ExecutionControl(
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=self.settings.job_deadline_seconds)
        )
        repo_manager = self.repo_manager or GitRepositoryManager(
            self.settings.github_workspace_root,
            execution_control=control,
        )
        checkout_path: Path | None = None
        try:
            self.repository_policy.require_allowed(job.repo_full_name)
            control.checkpoint("before-local-clone")
            checkout_path = repo_manager.clone_repository(
                repo_full_name=job.repo_full_name,
                ref=job.default_branch,
                token=self.settings.github_token,
                checkout_label=job.job_id,
            )
            control.checkpoint("after-local-clone")
            target_path = self._resolve_target_path(job, checkout_path)
            target_file = _repo_relative_path(checkout_path, target_path)
            tests_path = _repo_relative_path(checkout_path, job.tests_path)
            if not target_file.is_file():
                raise ValueError(f"Target file does not exist in checkout: {target_path}")
            if not tests_path.exists():
                raise ValueError(f"Tests path does not exist in checkout: {job.tests_path}")

            orchestrator = RefactorOrchestrator(
                llm_client=self.llm_factory(),
                run_root=self.settings.run_root,
                store=SQLiteRunStore(self.settings.resolved_database_path),
                pytest_timeout_seconds=self.settings.pytest_timeout_seconds,
                sandbox_backend=self.settings.sandbox_backend,
                sandbox_docker_image=self.settings.sandbox_docker_image,
                sandbox_memory=self.settings.sandbox_memory,
                sandbox_cpus=self.settings.sandbox_cpus,
                graph_backend=self.settings.graph_backend,
            )
            run_result = orchestrator.run(
                RefactorRequest(
                    target_file=target_file,
                    issue_text=job.issue_text,
                    tests_path=tests_path,
                    repo_name=job.repo_full_name,
                    issue_id=job.job_id,
                    max_retry=self.settings.max_retry,
                    allowed_import_roots=self.settings.allowed_import_roots,
                ),
                execution_control=control,
            )
            status = "DRY_RUN" if run_result.record.status == "SUCCESS" else "FAILED"
            return GitHubAutomationResult(
                job_id=job.job_id,
                repo_full_name=job.repo_full_name,
                issue_number=None,
                run_id=run_result.record.run_id,
                status=status,
                workspace_path=checkout_path,
                error=run_result.record.error if status == "FAILED" else None,
            )
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            raise
        except Exception as exc:
            return GitHubAutomationResult(
                job_id=job.job_id,
                repo_full_name=job.repo_full_name,
                issue_number=None,
                status="FAILED",
                workspace_path=checkout_path,
                error=str(exc),
            )
        finally:
            if checkout_path is not None and not self.settings.retain_checkouts:
                repo_manager.cleanup(checkout_path)

    def _resolve_target_path(self, job: GitHubRefactorJob, checkout_path: Path) -> str:
        if job.target_path != AUTO_TARGET_PATH:
            return job.target_path
        located = locate_source_file(checkout_path, job.issue_text)
        if located is None:
            raise ValueError(
                "Could not auto-locate a Python source file from the request. "
                "Provide a target file path."
            )
        return located.path

    def _default_llm_factory(self) -> RefactorClient:
        return MockRefactorClient() if self.settings.mock_llm else DeepSeekClient()
