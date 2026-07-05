from pathlib import Path

from refactor_agent.llm import MockRefactorClient
from refactor_agent.models import LLMRefactorResult, MetricsSnapshot, RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


class AlwaysBrokenClient:
    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        return LLMRefactorResult(
            thought="broken",
            fixed_code="def is_leap_year(year):\n    return True\n",
            insult_review="still broken",
        )


def test_orchestrator_self_heals_after_failed_attempt(tmp_path: Path):
    project = _make_leap_project(tmp_path)
    request = RefactorRequest(
        target_file=project / "leap_year.py",
        issue_text="1900 should not be a leap year",
        tests_path=project / "tests",
        repo_name="leap",
        max_retry=3,
    )
    orchestrator = RefactorOrchestrator(
        llm_client=MockRefactorClient(fail_times=1),
        run_root=tmp_path / ".runs",
        store=SQLiteRunStore(tmp_path / ".runs" / "runs.sqlite"),
    )
    result = orchestrator.run(request)
    assert result.record.status == "SUCCESS"
    assert result.record.self_heal_count == 1
    assert (result.workspace_path / "leap_year.py").read_text(encoding="utf-8") != (
        project / "leap_year.py"
    ).read_text(encoding="utf-8")


def test_orchestrator_fails_after_max_retry(tmp_path: Path):
    project = _make_leap_project(tmp_path)
    request = RefactorRequest(
        target_file=project / "leap_year.py",
        issue_text="1900 should not be a leap year",
        tests_path=project / "tests",
        repo_name="leap",
        max_retry=2,
    )
    orchestrator = RefactorOrchestrator(
        llm_client=AlwaysBrokenClient(),
        run_root=tmp_path / ".runs",
        store=SQLiteRunStore(tmp_path / ".runs" / "runs.sqlite"),
    )
    result = orchestrator.run(request)
    assert result.record.status == "FAILED"
    assert result.record.self_heal_count == 2


def _make_leap_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "leap_year.py").write_text(
        "def is_leap_year(year):\n"
        "    if year % 4 == 0:\n"
        "        if year % 100 == 0:\n"
        "            return True\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    (tests / "test_leap_year.py").write_text(
        "from leap_year import is_leap_year\n\n\n"
        "def test_leap_year_rules():\n"
        "    assert is_leap_year(2000) is True\n"
        "    assert is_leap_year(2024) is True\n"
        "    assert is_leap_year(1900) is False\n"
        "    assert is_leap_year(2023) is False\n",
        encoding="utf-8",
    )
    return project
