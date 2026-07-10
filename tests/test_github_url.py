from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from refactor_agent.github_url import GitHubUrlError, checkout_github_url


def test_checkout_github_url_clones_and_resolves_paths(tmp_path: Path):
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        checkout = Path(command[-1])
        (checkout / "src").mkdir(parents=True)
        (checkout / "src" / "app.py").write_text("def ok():\n    return True\n", encoding="utf-8")
        (checkout / "tests").mkdir()
        return subprocess.CompletedProcess(command, 0, "", "")

    checkout = checkout_github_url(
        repo_url="https://github.com/octo/demo.git",
        workspace_root=tmp_path / "github",
        target_path="src/app.py",
        tests_path="tests",
        branch="main",
        runner=fake_run,
    )

    assert checkout.repo_name == "octo/demo"
    assert checkout.target_file.is_file()
    assert checkout.tests_path.is_dir()
    assert calls[0][0][:6] == ["git", "clone", "--depth", "1", "--branch", "main"]
    assert calls[0][1] == (tmp_path / "github").resolve()


def test_checkout_github_url_reports_missing_target(tmp_path: Path):
    def fake_run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        checkout = Path(command[-1])
        (checkout / "tests").mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(GitHubUrlError, match="Target file does not exist"):
        checkout_github_url(
            repo_url="git@github.com:octo/demo.git",
            workspace_root=tmp_path / "github",
            target_path="src/app.py",
            tests_path="tests",
            runner=fake_run,
        )


def test_checkout_github_url_rejects_unsafe_paths(tmp_path: Path):
    def fake_run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        checkout = Path(command[-1])
        checkout.mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(GitHubUrlError, match="Unsafe repository path"):
        checkout_github_url(
            repo_url="https://github.com/octo/demo.git",
            workspace_root=tmp_path / "github",
            target_path="../secret.py",
            runner=fake_run,
        )


def test_checkout_github_url_redacts_credentials_in_git_errors(tmp_path: Path):
    def fake_run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            128,
            command,
            stderr="fatal: authentication failed",
        )

    with pytest.raises(GitHubUrlError) as exc_info:
        checkout_github_url(
            repo_url="https://x-access-token:secret@github.com/octo/demo.git",
            workspace_root=tmp_path / "github",
            target_path="src/app.py",
            runner=fake_run,
        )

    message = str(exc_info.value)
    assert "secret" not in message
    assert "***@github.com" in message
