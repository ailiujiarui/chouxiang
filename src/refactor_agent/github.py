from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from refactor_agent.config import AppSettings
from refactor_agent.llm import DeepSeekClient, MockRefactorClient, RefactorClient
from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob, RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


class GitHubAutomationError(RuntimeError):
    pass


CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
LLMFactory = Callable[[], RefactorClient]


class GitRepositoryManager:
    def __init__(self, workspace_root: Path, runner: CommandRunner | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.runner = runner or self._run
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def clone_for_issue(
        self,
        repo_full_name: str,
        clone_url: str,
        base_branch: str,
        issue_number: int,
    ) -> Path:
        checkout_name = f"{_safe_name(repo_full_name)}__issue-{issue_number}__{int(time.time())}"
        checkout_path = self.workspace_root / checkout_name
        self._assert_inside_workspace(checkout_path)
        self.runner(["git", "clone", "--depth", "1", "--branch", base_branch, clone_url, str(checkout_path)], self.workspace_root)
        return checkout_path

    def create_branch(self, checkout_path: Path, branch_name: str) -> None:
        self.runner(["git", "checkout", "-b", branch_name], checkout_path)

    def commit_and_push(self, checkout_path: Path, file_path: str, branch_name: str, message: str) -> None:
        self.runner(["git", "add", file_path], checkout_path)
        status = self.runner(["git", "status", "--porcelain", "--", file_path], checkout_path)
        if not status.stdout.strip():
            raise GitHubAutomationError("No file changes detected; refusing to create an empty PR.")
        self.runner(["git", "commit", "-m", message], checkout_path)
        self.runner(["git", "push", "--set-upstream", "origin", branch_name], checkout_path)

    def _assert_inside_workspace(self, path: Path) -> None:
        resolved = path.resolve()
        if not str(resolved).lower().startswith(str(self.workspace_root).lower()):
            raise GitHubAutomationError(f"Refusing to use checkout outside workspace root: {resolved}")

    @staticmethod
    def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:
            raise GitHubAutomationError("git executable was not found on PATH.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            detail = stderr or stdout or str(exc)
            raise GitHubAutomationError(f"git command failed: {' '.join(command)}\n{detail}") from exc


class GitHubApiClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def create_pull_request(self, repo_full_name: str, title: str, head: str, base: str, body: str) -> str:
        response = httpx.post(
            f"{self.api_url}/repos/{repo_full_name}/pulls",
            headers=self._headers(),
            json={"title": title, "head": head, "base": base, "body": body},
            timeout=30.0,
        )
        if response.status_code not in {200, 201}:
            raise GitHubAutomationError(f"GitHub PR creation failed: {response.status_code} {response.text}")
        return str(response.json()["html_url"])

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> None:
        response = httpx.post(
            f"{self.api_url}/repos/{repo_full_name}/issues/{issue_number}/comments",
            headers=self._headers(),
            json={"body": body},
            timeout=30.0,
        )
        if response.status_code not in {200, 201}:
            raise GitHubAutomationError(f"GitHub issue comment failed: {response.status_code} {response.text}")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }


class GitHubAutomationService:
    def __init__(
        self,
        settings: AppSettings,
        repo_manager: GitRepositoryManager | None = None,
        api_client: GitHubApiClient | None = None,
        llm_factory: LLMFactory | None = None,
    ) -> None:
        self.settings = settings
        self.repo_manager = repo_manager
        self.api_client = api_client
        self.llm_factory = llm_factory or self._default_llm_factory

    def process(self, job: GitHubRefactorJob) -> GitHubAutomationResult:
        branch_name = _branch_name(job.issue_number)
        try:
            repo_manager = self.repo_manager or GitRepositoryManager(self.settings.github_workspace_root)
            token = self.settings.github_token
            clone_url = _authenticated_clone_url(job.clone_url, token) if token else job.clone_url
            checkout_path = repo_manager.clone_for_issue(
                repo_full_name=job.repo_full_name,
                clone_url=clone_url,
                base_branch=job.default_branch,
                issue_number=job.issue_number,
            )
            repo_manager.create_branch(checkout_path, branch_name)
            target_file = _repo_relative_path(checkout_path, job.target_path)
            tests_path = _repo_relative_path(checkout_path, job.tests_path)
            if not target_file.is_file():
                raise GitHubAutomationError(f"Target file does not exist in checkout: {job.target_path}")
            if not tests_path.exists():
                raise GitHubAutomationError(f"Tests path does not exist in checkout: {job.tests_path}")

            orchestrator = RefactorOrchestrator(
                llm_client=self.llm_factory(),
                run_root=self.settings.run_root,
                store=SQLiteRunStore(self.settings.resolved_database_path),
                pytest_timeout_seconds=self.settings.pytest_timeout_seconds,
            )
            run_result = orchestrator.run(
                RefactorRequest(
                    target_file=target_file,
                    issue_text=job.issue_text,
                    tests_path=tests_path,
                    repo_name=job.repo_full_name,
                    issue_id=str(job.issue_number),
                    max_retry=self.settings.max_retry,
                )
            )
            if run_result.record.status != "SUCCESS" or run_result.candidate_file is None:
                self._comment_if_enabled(job, run_result.report_markdown)
                return GitHubAutomationResult(
                    repo_full_name=job.repo_full_name,
                    issue_number=job.issue_number,
                    branch_name=branch_name,
                    run_id=run_result.record.run_id,
                    status="FAILED",
                    workspace_path=checkout_path,
                    error=run_result.record.error or "refactor run failed",
                )

            target_file.write_text(run_result.candidate_file.read_text(encoding="utf-8"), encoding="utf-8")
            if self.settings.dry_run:
                return GitHubAutomationResult(
                    repo_full_name=job.repo_full_name,
                    issue_number=job.issue_number,
                    branch_name=branch_name,
                    run_id=run_result.record.run_id,
                    status="DRY_RUN",
                    workspace_path=checkout_path,
                )

            if not token:
                raise GitHubAutomationError("GITHUB_TOKEN is required when dry_run is disabled.")
            api_client = self.api_client or GitHubApiClient(token, self.settings.github_api_url)
            commit_message = f"fix: refactor issue #{job.issue_number}"
            repo_manager.commit_and_push(checkout_path, job.target_path, branch_name, commit_message)
            pr_url = api_client.create_pull_request(
                repo_full_name=job.repo_full_name,
                title=f"Refactor Agent fix for #{job.issue_number}: {job.issue_title}",
                head=branch_name,
                base=job.default_branch,
                body=run_result.report_markdown,
            )
            return GitHubAutomationResult(
                repo_full_name=job.repo_full_name,
                issue_number=job.issue_number,
                branch_name=branch_name,
                run_id=run_result.record.run_id,
                status="SUCCESS",
                pr_url=pr_url,
                workspace_path=checkout_path,
            )
        except Exception as exc:
            return GitHubAutomationResult(
                repo_full_name=job.repo_full_name,
                issue_number=job.issue_number,
                branch_name=branch_name,
                status="FAILED",
                error=str(exc),
            )

    def _comment_if_enabled(self, job: GitHubRefactorJob, body: str) -> None:
        if self.settings.dry_run or not self.settings.github_token:
            return
        api_client = self.api_client or GitHubApiClient(self.settings.github_token, self.settings.github_api_url)
        api_client.create_issue_comment(job.repo_full_name, job.issue_number, body)

    def _default_llm_factory(self) -> RefactorClient:
        return MockRefactorClient() if self.settings.mock_llm else DeepSeekClient()


def _repo_relative_path(checkout_path: Path, relative_path: str) -> Path:
    posix = PurePosixPath(relative_path.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts:
        raise GitHubAutomationError(f"Unsafe repository path: {relative_path}")
    return checkout_path / Path(*posix.parts)


def _branch_name(issue_number: int) -> str:
    return f"refactor-agent/issue-{issue_number}-{int(time.time())}"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "repo"


def _authenticated_clone_url(clone_url: str, token: str) -> str:
    if not clone_url.startswith("https://"):
        return clone_url
    return clone_url.replace("https://", f"https://x-access-token:{quote(token, safe='')}@", 1)
