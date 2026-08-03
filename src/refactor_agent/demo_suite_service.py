from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from refactor_agent.demo_cases import DemoCase, get_demo_case, materialize_demo_case
from refactor_agent.demo_suite import DEFAULT_DEMO_SUITE_CASES, DemoSuiteRun
from refactor_agent.local_refactor import run_local_refactor
from refactor_agent.models import RefactorRequest, RefactorRunResult


class DemoSuiteCaseError(ValueError):
    """Raised when a requested demo case cannot be selected or materialized."""


CaseStarted = Callable[[DemoCase], None]
CaseCompleted = Callable[[DemoSuiteRun], None]


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


def run_demo_suite(
    cases: Sequence[str] | None,
    *,
    run_root: Path,
    database_path: Path,
    max_retry: int,
    pytest_timeout_seconds: float,
    deadline_seconds: int,
    sandbox_backend: str,
    sandbox_docker_image: str,
    real_api: bool,
    mock_fail_times: int,
    dramatic_retry: bool,
    graph_backend: str = "langgraph",
    on_case_started: CaseStarted | None = None,
    on_case_completed: CaseCompleted | None = None,
    run_refactor: RefactorRunner = run_local_refactor,
) -> list[DemoSuiteRun]:
    """Execute selected demo cases without depending on CLI output or exit handling."""

    selected_cases = tuple(cases or DEFAULT_DEMO_SUITE_CASES)
    suite_runs: list[DemoSuiteRun] = []
    for case_name in selected_cases:
        try:
            target, issue, tests = materialize_demo_case(case_name, run_root)
            selected = get_demo_case(case_name)
        except ValueError as exc:
            raise DemoSuiteCaseError(str(exc)) from exc

        if on_case_started is not None:
            on_case_started(selected)
        request = RefactorRequest(
            target_file=target,
            issue_text=issue.read_text(encoding="utf-8"),
            tests_path=tests,
            repo_name=f"demo-{selected.name}",
            max_retry=max_retry,
        )
        result = run_refactor(
            request,
            run_root=run_root,
            database_path=database_path,
            pytest_timeout_seconds=pytest_timeout_seconds,
            mock=not real_api,
            sandbox_backend=sandbox_backend,
            sandbox_docker_image=sandbox_docker_image,
            mock_fail_times=suite_mock_fail_times(
                case_name,
                mock_fail_times,
                real_api,
                dramatic_retry,
            ),
            graph_backend=graph_backend,
            deadline_seconds=deadline_seconds,
        )
        suite_run = DemoSuiteRun(
            case_name=selected.name,
            title=selected.title,
            result=result,
        )
        suite_runs.append(suite_run)
        if on_case_completed is not None:
            on_case_completed(suite_run)
    return suite_runs


def suite_mock_fail_times(
    case_name: str,
    configured_fail_times: int,
    real_api: bool,
    dramatic_retry: bool,
) -> int:
    if real_api or not dramatic_retry:
        return configured_fail_times
    if case_name == "adversarial-weekend":
        return max(configured_fail_times, 1)
    return configured_fail_times
