from __future__ import annotations

from pathlib import Path

import pytest

from refactor_agent.demo_suite_service import (
    DemoSuiteCaseError,
    run_demo_suite,
    suite_mock_fail_times,
)
from refactor_agent.models import RefactorRunResult, RunRecord


def test_demo_suite_service_executes_cases_with_progress_callbacks(
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    progress: list[tuple[str, str]] = []

    def fake_run_refactor(request, **kwargs):
        calls.append((request, kwargs))
        return _result(tmp_path, request.repo_name or "unknown")

    runs = run_demo_suite(
        ["add-maze", "adversarial-weekend"],
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        max_retry=4,
        pytest_timeout_seconds=12.5,
        deadline_seconds=321,
        sandbox_backend="docker",
        sandbox_docker_image="sandbox:test",
        real_api=False,
        mock_fail_times=0,
        dramatic_retry=True,
        graph_backend="loop",
        on_case_started=lambda case: progress.append(("started", case.name)),
        on_case_completed=lambda item: progress.append(("completed", item.case_name)),
        run_refactor=fake_run_refactor,
    )

    assert [item.case_name for item in runs] == ["add-maze", "adversarial-weekend"]
    assert progress == [
        ("started", "add-maze"),
        ("completed", "add-maze"),
        ("started", "adversarial-weekend"),
        ("completed", "adversarial-weekend"),
    ]
    assert [request.repo_name for request, _ in calls] == [
        "demo-add-maze",
        "demo-adversarial-weekend",
    ]
    assert [options["mock_fail_times"] for _, options in calls] == [0, 1]
    assert all(request.max_retry == 4 for request, _ in calls)
    assert all(
        options
        == {
            "run_root": tmp_path / "runs",
            "database_path": tmp_path / "runs.sqlite",
            "pytest_timeout_seconds": 12.5,
            "mock": True,
            "sandbox_backend": "docker",
            "sandbox_docker_image": "sandbox:test",
            "mock_fail_times": options["mock_fail_times"],
            "graph_backend": "loop",
            "deadline_seconds": 321,
        }
        for _, options in calls
    )


def test_demo_suite_service_uses_default_cases_and_real_provider(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_refactor(request, **kwargs):
        calls.append(kwargs)
        return _result(tmp_path, request.repo_name or "unknown")

    runs = run_demo_suite(
        None,
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        max_retry=3,
        pytest_timeout_seconds=30,
        deadline_seconds=900,
        sandbox_backend="subprocess",
        sandbox_docker_image="sandbox:test",
        real_api=True,
        mock_fail_times=2,
        dramatic_retry=True,
        run_refactor=fake_run_refactor,
    )

    assert [item.case_name for item in runs] == [
        "add-maze",
        "adversarial-weekend",
        "business-day",
    ]
    assert all(options["mock"] is False for options in calls)
    assert all(options["mock_fail_times"] == 2 for options in calls)


def test_demo_suite_service_wraps_only_case_selection_errors(tmp_path: Path) -> None:
    called = False

    def fake_run_refactor(request, **kwargs):
        nonlocal called
        called = True
        return _result(tmp_path, "unexpected")

    with pytest.raises(DemoSuiteCaseError, match="Unknown demo case"):
        run_demo_suite(
            ["missing-case"],
            run_root=tmp_path / "runs",
            database_path=tmp_path / "runs.sqlite",
            max_retry=3,
            pytest_timeout_seconds=30,
            deadline_seconds=900,
            sandbox_backend="subprocess",
            sandbox_docker_image="sandbox:test",
            real_api=False,
            mock_fail_times=0,
            dramatic_retry=True,
            run_refactor=fake_run_refactor,
        )

    assert called is False


def test_suite_mock_fail_times_preserves_demo_retry_policy() -> None:
    assert suite_mock_fail_times("adversarial-weekend", 0, False, True) == 1
    assert suite_mock_fail_times("add-maze", 0, False, True) == 0
    assert suite_mock_fail_times("adversarial-weekend", 0, True, True) == 0
    assert suite_mock_fail_times("adversarial-weekend", 2, False, True) == 2


def _result(tmp_path: Path, repo_name: str) -> RefactorRunResult:
    return RefactorRunResult(
        record=RunRecord(
            run_id=repo_name,
            repo_name=repo_name,
            self_heal_count=0,
            status="SUCCESS",
        ),
        report_markdown=f"report for {repo_name}",
        workspace_path=tmp_path / repo_name,
        attempts=1,
    )
