from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from refactor_agent.github_url import (
    GitHubUrlCheckout,
    GitHubUrlError,
    checkout_github_url,
)
from refactor_agent.local_refactor import run_local_refactor
from refactor_agent.models import RefactorRequest, RefactorRunResult


class GitHubUrlCheckoutError(GitHubUrlError):
    """Raised when the read-only GitHub URL checkout phase fails."""


class CheckoutRunner(Protocol):
    def __call__(
        self,
        *,
        repo_url: str,
        workspace_root: Path,
        target_path: str,
        tests_path: str,
        branch: str | None,
    ) -> GitHubUrlCheckout: ...


class RefactorRunner(Protocol):
    def __call__(
        self,
        request: RefactorRequest,
        *,
        run_root: Path,
        database_path: Path,
        pytest_timeout_seconds: float,
        mock: bool,
        sandbox_backend: str,
        sandbox_docker_image: str,
        mock_fail_times: int,
        graph_backend: str,
        deadline_seconds: int,
    ) -> RefactorRunResult: ...


@dataclass(frozen=True)
class GitHubUrlSubmissionResult:
    checkout: GitHubUrlCheckout
    run_result: RefactorRunResult


def execute_github_url_submission(
    *,
    repo_url: str,
    target_path: str,
    issue_text: str,
    tests_path: str,
    branch: str | None,
    repo_name: str | None,
    max_retry: int,
    github_workspace_root: Path,
    run_root: Path,
    database_path: Path,
    pytest_timeout_seconds: float,
    deadline_seconds: int,
    mock: bool,
    sandbox_backend: str,
    sandbox_docker_image: str,
    mock_fail_times: int,
    graph_backend: str = "langgraph",
    checkout_runner: CheckoutRunner = checkout_github_url,
    run_refactor: RefactorRunner = run_local_refactor,
) -> GitHubUrlSubmissionResult:
    """Checkout and refactor one GitHub URL without CLI presentation dependencies."""

    try:
        checkout = checkout_runner(
            repo_url=repo_url,
            workspace_root=github_workspace_root,
            target_path=target_path,
            tests_path=tests_path,
            branch=branch,
        )
    except GitHubUrlError as exc:
        raise GitHubUrlCheckoutError(str(exc)) from exc

    request = RefactorRequest(
        target_file=checkout.target_file,
        issue_text=issue_text,
        tests_path=checkout.tests_path,
        repo_name=repo_name or checkout.repo_name,
        max_retry=max_retry,
    )
    result = run_refactor(
        request,
        run_root=run_root,
        database_path=database_path,
        pytest_timeout_seconds=pytest_timeout_seconds,
        mock=mock,
        sandbox_backend=sandbox_backend,
        sandbox_docker_image=sandbox_docker_image,
        mock_fail_times=mock_fail_times,
        graph_backend=graph_backend,
        deadline_seconds=deadline_seconds,
    )
    return GitHubUrlSubmissionResult(checkout=checkout, run_result=result)
