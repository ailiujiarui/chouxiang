from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from refactor_agent.cli import app
from refactor_agent.demo_cases import get_demo_case
from refactor_agent.demo_suite import DemoSuiteRun
from refactor_agent.demo_suite_service import DemoSuiteCaseError
from refactor_agent.local_refactor import LocalRefactorConfigurationError
from refactor_agent.models import RefactorRunResult, RunRecord


runner = CliRunner()


def test_demo_suite_cli_preserves_progress_report_and_service_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    suite_run = _suite_run(tmp_path, "SUCCESS")

    def fake_execute(cases, **options):
        captured["cases"] = cases
        captured["options"] = options
        options["on_case_started"](get_demo_case("add-maze"))
        options["on_case_completed"](suite_run)
        return [suite_run]

    monkeypatch.setattr("refactor_agent.cli.execute_demo_suite", fake_execute)
    result = runner.invoke(
        app,
        [
            "demo-suite",
            "--case",
            "add-maze",
            "--run-root",
            str(tmp_path / "runs"),
            "--database",
            str(tmp_path / "runs.sqlite"),
            "--sandbox-backend",
            "docker",
            "--full-report",
        ],
    )

    assert result.exit_code == 0
    assert "=== 路演案例: add-maze" in result.stdout
    assert "完成: 成功 | 自愈 0 轮" in result.stdout
    assert "case report" in result.stdout
    assert "路演总战报" in result.stdout
    assert captured["cases"] == ["add-maze"]
    options = captured["options"]
    assert options["run_root"] == tmp_path / "runs"
    assert options["database_path"] == tmp_path / "runs.sqlite"
    assert options["sandbox_backend"] == "docker"


def test_demo_suite_cli_returns_failure_when_any_case_fails(monkeypatch, tmp_path: Path) -> None:
    suite_run = _suite_run(tmp_path, "FAILED")
    monkeypatch.setattr(
        "refactor_agent.cli.execute_demo_suite",
        lambda cases, **options: [suite_run],
    )

    result = runner.invoke(app, ["demo-suite", "--case", "add-maze"])

    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("error", "expected_exit"),
    [
        (DemoSuiteCaseError("Unknown demo case: missing"), 2),
        (LocalRefactorConfigurationError("DeepSeek configuration is missing"), 1),
    ],
)
def test_demo_suite_cli_preserves_service_error_translation(
    monkeypatch,
    error: Exception,
    expected_exit: int,
) -> None:
    def fail(cases, **options):
        raise error

    monkeypatch.setattr("refactor_agent.cli.execute_demo_suite", fail)

    result = runner.invoke(app, ["demo-suite", "--case", "missing"])

    assert result.exit_code == expected_exit
    assert str(error) in result.stdout


def _suite_run(tmp_path: Path, status: str) -> DemoSuiteRun:
    result = RefactorRunResult(
        record=RunRecord(
            run_id=f"demo-{status.lower()}",
            repo_name="demo-add-maze",
            self_heal_count=0,
            status=status,
        ),
        report_markdown="case report",
        workspace_path=tmp_path / "workspace",
        attempts=1,
    )
    return DemoSuiteRun(case_name="add-maze", title="Addition maze", result=result)
