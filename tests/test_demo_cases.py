from pathlib import Path

from refactor_agent.demo_cases import DEMO_CASE_NAMES, materialize_demo_case
from refactor_agent.demo_suite import DEFAULT_DEMO_SUITE_CASES, DemoSuiteRun, render_demo_suite_report
from refactor_agent.cli import _suite_mock_fail_times
from refactor_agent.llm import MockRefactorClient
from refactor_agent.metrics import analyze_file
from refactor_agent.models import RefactorRequest, RefactorRunResult, RunRecord


def test_materialize_demo_case_writes_project(tmp_path: Path):
    target, issue, tests = materialize_demo_case("add-maze", tmp_path / ".runs")

    assert target.name == "math_maze.py"
    assert issue.is_file()
    assert (tests / "test_math_maze.py").is_file()
    assert "def add" in target.read_text(encoding="utf-8")


def test_mock_refactor_client_supports_all_demo_cases(tmp_path: Path):
    for name in DEMO_CASE_NAMES:
        target, issue, tests = materialize_demo_case(name, tmp_path / name)
        code = target.read_text(encoding="utf-8")
        result = MockRefactorClient().refactor(
            request=RefactorRequest(
                target_file=target,
                issue_text=issue.read_text(encoding="utf-8"),
                tests_path=tests,
            ),
            current_code=code,
            baseline_metrics=analyze_file(target),
            previous_error=None,
            attempt=1,
        )

        assert result.fixed_code.startswith("def ")
        assert result.insult_review


def test_default_demo_suite_cases_exist():
    assert DEFAULT_DEMO_SUITE_CASES
    assert all(name in DEMO_CASE_NAMES for name in DEFAULT_DEMO_SUITE_CASES)
    assert "adversarial-weekend" in DEFAULT_DEMO_SUITE_CASES


def test_render_demo_suite_report_contains_battle_summary(tmp_path: Path):
    result = RefactorRunResult(
        record=RunRecord(
            run_id="202607090001-demo",
            repo_name="demo-add-maze",
            pre_loc=20,
            post_loc=2,
            pre_cc=8,
            post_cc=1,
            self_heal_count=0,
            status="SUCCESS",
        ),
        report_markdown="case report",
        workspace_path=tmp_path / ".runs" / "202607090001-demo" / "workspace",
        attempts=1,
    )
    report = render_demo_suite_report(
        [DemoSuiteRun(case_name="add-maze", title="Addition maze", result=result)],
        run_root=tmp_path / ".runs",
        database=tmp_path / ".runs" / "refactor_agent.sqlite",
    )

    assert "路演总战报" in report
    assert "案例对比表" in report
    assert "现场串场词" in report
    assert "| add-maze | 成功 | 0 | 20 -> 2 (-18) | 8 -> 1 (-7) | n/a |" in report


def test_suite_mock_fail_times_forces_adversarial_retry_only_in_mock_mode():
    assert _suite_mock_fail_times("adversarial-weekend", 0, real_api=False, dramatic_retry=True) == 1
    assert _suite_mock_fail_times("add-maze", 0, real_api=False, dramatic_retry=True) == 0
    assert _suite_mock_fail_times("adversarial-weekend", 0, real_api=True, dramatic_retry=True) == 0
    assert _suite_mock_fail_times("adversarial-weekend", 2, real_api=False, dramatic_retry=True) == 2
