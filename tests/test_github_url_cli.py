from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from refactor_agent.cli import app
from refactor_agent.github_url_submission import (
    GitHubUrlCheckoutError,
    GitHubUrlSubmissionResult,
)
from refactor_agent.local_refactor import LocalRefactorConfigurationError
from refactor_agent.github_url import GitHubUrlCheckout
from refactor_agent.models import RefactorRunResult, RunRecord


runner = CliRunner()


def test_github_url_cli_preserves_report_paths_and_service_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    submission = _submission(tmp_path, status="SUCCESS")

    def fake_execute(**options):
        captured.update(options)
        return submission

    monkeypatch.setattr("refactor_agent.cli.execute_github_url_submission", fake_execute)
    result = runner.invoke(
        app,
        [
            "github-url",
            "--repo-url",
            "https://github.com/owner/repository.git",
            "--target",
            "src/module.py",
            "--issue-text",
            "Simplify",
            "--tests",
            "tests",
            "--mock",
            "--run-root",
            str(tmp_path / "runs"),
            "--database",
            str(tmp_path / "runs.sqlite"),
        ],
    )

    assert result.exit_code == 0
    assert "github url report" in result.stdout
    assert "克隆仓库:" in result.stdout
    assert "checkout" in result.stdout
    assert "优化候选文件:" in result.stdout
    assert "candidate.py" in result.stdout
    assert captured["issue_text"] == "Simplify"
    assert captured["database_path"] == tmp_path / "runs.sqlite"
    assert captured["mock"] is True


@pytest.mark.parametrize(
    ("error", "expected_exit"),
    [
        (GitHubUrlCheckoutError("clone failed"), 2),
        (LocalRefactorConfigurationError("DeepSeek configuration is missing"), 1),
    ],
)
def test_github_url_cli_preserves_error_translation(
    monkeypatch,
    error: Exception,
    expected_exit: int,
) -> None:
    def fail(**options):
        raise error

    monkeypatch.setattr("refactor_agent.cli.execute_github_url_submission", fail)
    result = runner.invoke(
        app,
        [
            "github-url",
            "--repo-url",
            "https://github.com/owner/repository.git",
            "--target",
            "src/module.py",
            "--issue-text",
            "Simplify",
        ],
    )

    assert result.exit_code == expected_exit
    assert str(error) in result.stdout


def _submission(tmp_path: Path, status: str) -> GitHubUrlSubmissionResult:
    checkout = GitHubUrlCheckout(
        repo_url="https://github.com/owner/repository.git",
        checkout_path=tmp_path / "checkout",
        target_file=tmp_path / "checkout" / "src" / "module.py",
        tests_path=tmp_path / "checkout" / "tests",
        repo_name="owner/repository",
    )
    run_result = RefactorRunResult(
        record=RunRecord(
            run_id="run-1",
            repo_name="owner/repository",
            self_heal_count=0,
            status=status,
        ),
        report_markdown="github url report",
        workspace_path=tmp_path / "workspace",
        candidate_file=tmp_path / "candidate.py",
        attempts=1,
    )
    return GitHubUrlSubmissionResult(checkout=checkout, run_result=run_result)
