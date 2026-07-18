from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from uuid import uuid4

from refactor_agent.execution_control import ExecutionControl
from refactor_agent.repository_allowlist import normalize_repository_identity


class GitHubAutomationError(RuntimeError):
    pass


CommandRunner = Callable[[list[str], Path, dict[str, str] | None], subprocess.CompletedProcess[str]]


class GitRepositoryManager:
    """Read-only canonical GitHub checkout manager."""

    def __init__(
        self,
        workspace_root: Path,
        runner: CommandRunner | None = None,
        execution_control: ExecutionControl | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.runner = runner or self._run
        self.execution_control = execution_control
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def clone_repository(
        self,
        repo_full_name: str,
        ref: str | None,
        token: str | None = None,
        checkout_label: str = "repository",
    ) -> Path:
        checkout_name = f"{_safe_name(checkout_label)}__{int(time.time())}-{uuid4().hex[:8]}"
        checkout_path = self.workspace_root / checkout_name
        self._assert_inside_workspace(checkout_path)
        clone_url = canonical_clone_url(repo_full_name)
        command = ["git", "clone", "--depth", "1"]
        if ref:
            command.extend(["--branch", ref])
        command.extend([clone_url, str(checkout_path)])
        with _git_auth_environment(token) as auth_env:
            self.runner(command, self.workspace_root, auth_env)
        origin = self.runner(["git", "remote", "get-url", "origin"], checkout_path, None).stdout.strip()
        if origin != clone_url or "@" in origin:
            raise GitHubAutomationError("Cloned repository origin failed canonical URL validation.")
        return checkout_path

    def cleanup(self, checkout_path: Path) -> None:
        self._assert_inside_workspace(checkout_path)
        if checkout_path.exists():
            import shutil

            shutil.rmtree(checkout_path)

    def _assert_inside_workspace(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace_root):
            raise GitHubAutomationError(f"Refusing to use checkout outside workspace root: {resolved}")

    def _run(
        self,
        command: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            timeout = (
                self.execution_control.bounded_timeout(60.0, "git-command")
                if self.execution_control is not None
                else 60.0
            )
            return subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
                env=env,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise GitHubAutomationError("git executable was not found on PATH.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            detail = stderr or stdout or str(exc)
            raise GitHubAutomationError(f"git command failed: {' '.join(command)}\n{detail}") from exc


def _repo_relative_path(checkout_path: Path, relative_path: str) -> Path:
    posix = PurePosixPath(relative_path.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts:
        raise GitHubAutomationError(f"Unsafe repository path: {relative_path}")
    checkout = checkout_path.resolve()
    resolved = (checkout / Path(*posix.parts)).resolve()
    if not resolved.is_relative_to(checkout):
        raise GitHubAutomationError(f"Repository path escapes checkout through a symlink: {relative_path}")
    return resolved


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "repo"


def canonical_clone_url(repo_full_name: str) -> str:
    try:
        if "://" in repo_full_name:
            raise ValueError("repository identity must not be a URL")
        normalized = normalize_repository_identity(repo_full_name)
    except ValueError as exc:
        raise GitHubAutomationError(f"Invalid GitHub repository name: {repo_full_name!r}")
    return f"https://github.com/{normalized}.git"


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
            helper.write_text(
                '@echo off\r\necho %~1 | findstr /I "Username" >nul && '
                '(echo %GIT_ASKPASS_USERNAME%) || (echo %GIT_ASKPASS_PASSWORD%)\r\n',
                encoding="ascii",
            )
        else:
            helper = root / "askpass.sh"
            helper.write_text(
                '#!/bin/sh\ncase "$1" in *Username*) printf \'%s\\n\' '
                '"$GIT_ASKPASS_USERNAME";; *) printf \'%s\\n\' '
                '"$GIT_ASKPASS_PASSWORD";; esac\n',
                encoding="ascii",
            )
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
