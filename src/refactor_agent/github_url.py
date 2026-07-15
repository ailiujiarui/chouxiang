from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


class GitHubUrlError(RuntimeError):
    pass


CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GitHubUrlCheckout:
    repo_url: str
    checkout_path: Path
    target_file: Path
    tests_path: Path
    repo_name: str


def checkout_github_url(
    repo_url: str,
    workspace_root: Path,
    target_path: str,
    tests_path: str = "tests",
    branch: str | None = None,
    runner: CommandRunner | None = None,
) -> GitHubUrlCheckout:
    """Clone a GitHub repository URL into a local workspace and resolve repo-relative paths."""
    repo_url = repo_url.strip()
    if not repo_url:
        raise GitHubUrlError("Repository URL is required.")

    workspace = workspace_root.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    checkout_path = workspace / _checkout_name(repo_url)
    _assert_inside_workspace(workspace, checkout_path)

    command = ["git", "clone", "--depth", "1"]
    if branch:
        command.extend(["--branch", branch])
    command.extend([repo_url, str(checkout_path)])
    _run_command(command, workspace, runner or _run)

    target_file = _repo_relative_path(checkout_path, target_path)
    resolved_tests_path = _repo_relative_path(checkout_path, tests_path)
    if not target_file.is_file():
        raise GitHubUrlError(f"Target file does not exist in cloned repository: {target_path}")
    if not resolved_tests_path.exists():
        raise GitHubUrlError(f"Tests path does not exist in cloned repository: {tests_path}")

    return GitHubUrlCheckout(
        repo_url=repo_url,
        checkout_path=checkout_path,
        target_file=target_file,
        tests_path=resolved_tests_path,
        repo_name=_repo_name(repo_url),
    )


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True)


def _run_command(command: list[str], cwd: Path, runner: CommandRunner) -> subprocess.CompletedProcess[str]:
    try:
        return runner(command, cwd)
    except FileNotFoundError as exc:
        raise GitHubUrlError("git executable was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = _redact_text(exc.stderr.strip()) if exc.stderr else ""
        stdout = _redact_text(exc.stdout.strip()) if exc.stdout else ""
        detail = stderr or stdout or str(exc)
        raise GitHubUrlError(f"git command failed: {' '.join(_redact_command(command))}\n{detail}") from exc


def _checkout_name(repo_url: str) -> str:
    return f"{int(time.time())}-{uuid4().hex[:8]}-{_safe_name(_repo_name(repo_url))}"


def _repo_name(repo_url: str) -> str:
    cleaned = repo_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if ":" in cleaned and not cleaned.startswith(("http://", "https://")):
        cleaned = cleaned.rsplit(":", 1)[-1]
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else "repo"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "repo"


def _repo_relative_path(checkout_path: Path, relative_path: str) -> Path:
    posix = PurePosixPath(relative_path.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts:
        raise GitHubUrlError(f"Unsafe repository path: {relative_path}")
    return checkout_path / Path(*posix.parts)


def _assert_inside_workspace(workspace_root: Path, path: Path) -> None:
    resolved_root = workspace_root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise GitHubUrlError(f"Refusing to use checkout outside workspace root: {resolved_path}")


def _redact_command(command: list[str]) -> list[str]:
    return [_redact_url(part) for part in command]


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, f"***@{host}", parsed.path, parsed.query, parsed.fragment))


def _redact_text(value: str) -> str:
    return re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1***@", value)
