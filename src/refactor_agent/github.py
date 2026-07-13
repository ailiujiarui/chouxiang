from __future__ import annotations

import hashlib
import re
import os
import stat
import subprocess
import tempfile
import time
from uuid import uuid4
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from refactor_agent.config import AppSettings
from refactor_agent.locator import AUTO_TARGET_PATH, locate_source_file
from refactor_agent.llm import DeepSeekClient, MockRefactorClient, RefactorClient
from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob, RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


class GitHubAutomationError(RuntimeError):
    pass


CommandRunner = Callable[[list[str], Path, dict[str, str] | None], subprocess.CompletedProcess[str]]
LLMFactory = Callable[[], RefactorClient]


class GitRepositoryManager:
    def __init__(self, workspace_root: Path, runner: CommandRunner | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.runner = runner or self._run
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def clone_for_issue(
        self,
        repo_full_name: str,
        base_branch: str,
        issue_number: int,
        token: str | None = None,
    ) -> Path:
        checkout_name = f"{_safe_name(repo_full_name)}__issue-{issue_number}__{int(time.time())}-{uuid4().hex[:8]}"
        checkout_path = self.workspace_root / checkout_name
        self._assert_inside_workspace(checkout_path)
        clone_url = canonical_clone_url(repo_full_name)
        with _git_auth_environment(token) as auth_env:
            self.runner(
                ["git", "clone", "--depth", "1", "--branch", base_branch, clone_url, str(checkout_path)],
                self.workspace_root,
                auth_env,
            )
        origin = self.runner(["git", "remote", "get-url", "origin"], checkout_path, None).stdout.strip()
        if origin != clone_url or "@" in origin:
            raise GitHubAutomationError("Cloned repository origin failed canonical URL validation.")
        return checkout_path

    def create_branch(self, checkout_path: Path, branch_name: str) -> None:
        self.runner(["git", "checkout", "-b", branch_name], checkout_path, None)

    def commit_and_push(
        self,
        checkout_path: Path,
        file_path: str,
        branch_name: str,
        message: str,
        token: str,
    ) -> None:
        self.runner(["git", "config", "user.name", "Refactor Agent Bot"], checkout_path, None)
        self.runner(["git", "config", "user.email", "refactor-agent@users.noreply.github.com"], checkout_path, None)
        self.runner(["git", "add", file_path], checkout_path, None)
        status = self.runner(["git", "status", "--porcelain", "--", file_path], checkout_path, None)
        if not status.stdout.strip():
            raise GitHubAutomationError("No file changes detected; refusing to create an empty PR.")
        self.runner(["git", "commit", "-m", message], checkout_path, None)
        with _git_auth_environment(token) as auth_env:
            self.runner(["git", "push", "--set-upstream", "origin", branch_name], checkout_path, auth_env)

    def cleanup(self, checkout_path: Path) -> None:
        self._assert_inside_workspace(checkout_path)
        if checkout_path.exists():
            import shutil

            shutil.rmtree(checkout_path)

    def _assert_inside_workspace(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace_root):
            raise GitHubAutomationError(f"Refusing to use checkout outside workspace root: {resolved}")

    @staticmethod
    def _run(
        command: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True, env=env)
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
        branch_name = _branch_name(job.issue_number, job.job_id)
        repo_manager = self.repo_manager or GitRepositoryManager(self.settings.github_workspace_root)
        checkout_path: Path | None = None
        try:
            token = self.settings.github_token
            checkout_path = repo_manager.clone_for_issue(
                repo_full_name=job.repo_full_name,
                base_branch=job.default_branch,
                issue_number=job.issue_number,
                token=token,
            )
            repo_manager.create_branch(checkout_path, branch_name)
            target_path = self._resolve_target_path(job, checkout_path)
            target_file = _repo_relative_path(checkout_path, target_path)
            tests_path = _repo_relative_path(checkout_path, job.tests_path)
            if not target_file.is_file():
                raise GitHubAutomationError(f"Target file does not exist in checkout: {target_path}")
            if not tests_path.exists():
                raise GitHubAutomationError(f"Tests path does not exist in checkout: {job.tests_path}")

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
                    issue_id=str(job.issue_number),
                    max_retry=self.settings.max_retry,
                    allowed_import_roots=self.settings.allowed_import_roots,
                )
            )
            if run_result.record.status != "SUCCESS" or run_result.candidate_file is None:
                self._comment_if_enabled(job, run_result.report_markdown)
                return GitHubAutomationResult(
                    job_id=job.job_id,
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
                    job_id=job.job_id,
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
            repo_manager.commit_and_push(checkout_path, target_path, branch_name, commit_message, token)
            pr_url = api_client.create_pull_request(
                repo_full_name=job.repo_full_name,
                title=f"Refactor Agent fix for #{job.issue_number}: {job.issue_title}",
                head=branch_name,
                base=job.default_branch,
                body=run_result.report_markdown,
            )
            api_client.create_issue_comment(
                job.repo_full_name,
                job.issue_number,
                f"Refactor Agent completed the signed webhook run.\n\nPull request: {pr_url}\n\n{run_result.report_markdown}",
            )
            return GitHubAutomationResult(
                job_id=job.job_id,
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
                job_id=job.job_id,
                repo_full_name=job.repo_full_name,
                issue_number=job.issue_number,
                branch_name=branch_name,
                status="FAILED",
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
            raise GitHubAutomationError(
                "Could not auto-locate a Python source file from the issue text. "
                "Add 'target: path/to/file.py' to the issue."
            )
        return located.path

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
    checkout = checkout_path.resolve()
    resolved = (checkout / Path(*posix.parts)).resolve()
    if not resolved.is_relative_to(checkout):
        raise GitHubAutomationError(f"Repository path escapes checkout through a symlink: {relative_path}")
    return resolved


def _branch_name(issue_number: int, job_id: str) -> str:
    suffix = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return f"refactor-agent/issue-{issue_number}-{suffix}"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "repo"


def canonical_clone_url(repo_full_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_full_name):
        raise GitHubAutomationError(f"Invalid GitHub repository name: {repo_full_name!r}")
    return f"https://github.com/{repo_full_name}.git"


class _git_auth_environment:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self.directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> dict[str, str] | None:
        if not self.token:
            return None
        self.directory = tempfile.TemporaryDirectory(prefix="refactor-agent-git-auth-")
        root = Path(self.directory.name)
        if os.name == "nt":
            helper = root / "askpass.cmd"
            helper.write_text("@echo off\r\necho %~1 | findstr /I \"Username\" >nul && (echo %GIT_ASKPASS_USERNAME%) || (echo %GIT_ASKPASS_PASSWORD%)\r\n", encoding="ascii")
        else:
            helper = root / "askpass.sh"
            helper.write_text("#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' \"$GIT_ASKPASS_USERNAME\";; *) printf '%s\\n' \"$GIT_ASKPASS_PASSWORD\";; esac\n", encoding="ascii")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TMP", "TEMP"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(
            {
                "GIT_ASKPASS": str(helper),
                "GIT_ASKPASS_REQUIRE": "force",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS_USERNAME": "x-access-token",
                "GIT_ASKPASS_PASSWORD": self.token,
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "credential.useHttpPath",
                "GIT_CONFIG_VALUE_1": "true",
            }
        )
        return env

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.directory is not None:
            self.directory.cleanup()
