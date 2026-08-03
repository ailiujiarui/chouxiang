from __future__ import annotations

from pathlib import Path

import pytest

from refactor_agent.github_url import GitHubUrlCheckout, GitHubUrlError
from refactor_agent.github_url_submission import (
    GitHubUrlCheckoutError,
    execute_github_url_submission,
)
from refactor_agent.models import RefactorRunResult, RunRecord


def test_github_url_submission_builds_request_and_executes_local_refactor(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    checkout = _checkout(tmp_path)
    expected_result = _run_result(tmp_path)

    def fake_checkout(**options):
        captured["checkout"] = options
        return checkout

    def fake_run_refactor(request, **options):
        captured["request"] = request
        captured["run"] = options
        return expected_result

    submission = execute_github_url_submission(
        repo_url="https://github.com/owner/repository.git",
        target_path="src/module.py",
        issue_text="Simplify the module",
        tests_path="tests",
        branch="main",
        repo_name=None,
        max_retry=4,
        github_workspace_root=tmp_path / "checkouts",
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        pytest_timeout_seconds=12.5,
        deadline_seconds=321,
        mock=True,
        sandbox_backend="docker",
        sandbox_docker_image="sandbox:test",
        mock_fail_times=2,
        graph_backend="loop",
        checkout_runner=fake_checkout,
        run_refactor=fake_run_refactor,
    )

    request = captured["request"]
    assert submission.checkout is checkout
    assert submission.run_result is expected_result
    assert captured["checkout"] == {
        "repo_url": "https://github.com/owner/repository.git",
        "workspace_root": tmp_path / "checkouts",
        "target_path": "src/module.py",
        "tests_path": "tests",
        "branch": "main",
    }
    assert request.target_file == checkout.target_file
    assert request.tests_path == checkout.tests_path
    assert request.issue_text == "Simplify the module"
    assert request.repo_name == "owner/repository"
    assert request.max_retry == 4
    assert captured["run"] == {
        "run_root": tmp_path / "runs",
        "database_path": tmp_path / "runs.sqlite",
        "pytest_timeout_seconds": 12.5,
        "mock": True,
        "sandbox_backend": "docker",
        "sandbox_docker_image": "sandbox:test",
        "mock_fail_times": 2,
        "graph_backend": "loop",
        "deadline_seconds": 321,
    }


def test_github_url_submission_prefers_explicit_repository_name(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run_refactor(request, **options):
        captured["repo_name"] = request.repo_name
        return _run_result(tmp_path)

    execute_github_url_submission(
        repo_url="https://github.com/owner/repository.git",
        target_path="src/module.py",
        issue_text="Simplify",
        tests_path="tests",
        branch=None,
        repo_name="custom-name",
        max_retry=3,
        github_workspace_root=tmp_path / "checkouts",
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        pytest_timeout_seconds=30,
        deadline_seconds=900,
        mock=False,
        sandbox_backend="subprocess",
        sandbox_docker_image="sandbox:test",
        mock_fail_times=0,
        checkout_runner=lambda **options: _checkout(tmp_path),
        run_refactor=fake_run_refactor,
    )

    assert captured["repo_name"] == "custom-name"


def test_github_url_submission_wraps_only_checkout_errors(tmp_path: Path) -> None:
    called = False

    def fail_checkout(**options):
        raise GitHubUrlError("clone failed")

    def fake_run_refactor(request, **options):
        nonlocal called
        called = True
        return _run_result(tmp_path)

    with pytest.raises(GitHubUrlCheckoutError, match="clone failed"):
        execute_github_url_submission(
            repo_url="https://github.com/owner/repository.git",
            target_path="src/module.py",
            issue_text="Simplify",
            tests_path="tests",
            branch=None,
            repo_name=None,
            max_retry=3,
            github_workspace_root=tmp_path / "checkouts",
            run_root=tmp_path / "runs",
            database_path=tmp_path / "runs.sqlite",
            pytest_timeout_seconds=30,
            deadline_seconds=900,
            mock=True,
            sandbox_backend="subprocess",
            sandbox_docker_image="sandbox:test",
            mock_fail_times=0,
            checkout_runner=fail_checkout,
            run_refactor=fake_run_refactor,
        )

    assert called is False


def _checkout(tmp_path: Path) -> GitHubUrlCheckout:
    return GitHubUrlCheckout(
        repo_url="https://github.com/owner/repository.git",
        checkout_path=tmp_path / "checkout",
        target_file=tmp_path / "checkout" / "src" / "module.py",
        tests_path=tmp_path / "checkout" / "tests",
        repo_name="owner/repository",
    )


def _run_result(tmp_path: Path) -> RefactorRunResult:
    return RefactorRunResult(
        record=RunRecord(
            run_id="run-1",
            repo_name="owner/repository",
            self_heal_count=0,
            status="SUCCESS",
        ),
        report_markdown="github url report",
        workspace_path=tmp_path / "workspace",
        candidate_file=tmp_path / "candidate.py",
        attempts=1,
    )
