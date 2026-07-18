from pathlib import Path
import json

from refactor_agent.debate_state import validate_status_sequence
from refactor_agent.llm import LLMError, MockRefactorClient
from refactor_agent.models import LLMRefactorResult, MetricsSnapshot, RefactorRequest, TrajectoryMemoryRecord
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.sandbox import DockerStatus
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


class ExplodingClient:
    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        raise AssertionError("LLM should not be called when sandbox preflight fails")


class LLMFailingClient:
    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        raise LLMError("provider unavailable")


class CapturingClient:
    def __init__(self) -> None:
        self.issue_text: str | None = None

    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        self.issue_text = request.issue_text
        return LLMRefactorResult(
            thought="用一行布尔表达式保留闰年规则。",
            fixed_code=(
                "def is_leap_year(year):\n"
                "    return (year % 4 == 0) and (year % 100 != 0 or year % 400 == 0)\n"
            ),
            insult_review="旧代码把百年规则写成了小型岔路口，路牌还贴反了。",
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
    assert result.ast_validation is not None
    assert result.ast_validation.ok is True
    assert result.mutation_result is not None
    assert result.performance_profile is not None
    assert result.performance_profile.passed is True
    assert result.adversarial_result is not None
    assert result.adversarial_result.passed is True
    assert result.debate_rounds
    assert result.debate_rounds[-1].converged is True
    assert [message.agent for message in result.debate_rounds[-1].messages] == [
        "MINIMIZER",
        "DEFENDER",
        "DEFENDER",
        "ADVERSARY",
        "ADVERSARY",
        "ADVERSARY",
        "JUDGE",
    ]
    assert "Adversarial tests" in result.report_markdown
    assert "Mutation testing" in result.report_markdown
    assert "Multi-agent debate rounds" in result.report_markdown
    assert "Debate State Machine" in result.report_markdown
    assert "Peak traced memory" in result.report_markdown
    assert "指标对比表" in result.report_markdown
    assert "验证矩阵" in result.report_markdown
    assert "| LOC |" in result.report_markdown
    assert "| AST 守卫 (AST guard) |" in result.report_markdown
    assert "Selected AST targets" in result.report_markdown
    assert "Executed graph nodes" in result.report_markdown
    artifact_root = tmp_path / ".runs" / result.record.run_id / "artifacts"
    assert {path.name for path in artifact_root.iterdir()} == {
        "original.py",
        "candidate.py",
        "change.diff",
        "pytest.log",
        "adversary.log",
        "mutation.json",
        "report.md",
    }
    assert "PREPARE -> MINIMIZER -> AST_GUARD" in result.report_markdown
    assert result.ast_rewrite is not None
    assert result.ast_rewrite.allowed_regions == ["is_leap_year"]
    trajectory_path = tmp_path / ".runs" / result.record.run_id / "trajectory.jsonl"
    assert trajectory_path.is_file()
    statuses = [
        json.loads(line)["status"]
        for line in trajectory_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "MINIMIZER_PROPOSED" in statuses
    assert "DEFENDER_REVIEWED" in statuses
    assert "ADVERSARY_CRITIQUED" in statuses
    assert "ADVERSARY_CHALLENGED" in statuses
    assert "JUDGE_SCORED" in statuses
    assert "DEBATE_CONVERGED" in statuses
    assert validate_status_sequence(statuses) == []
    steps = [
        json.loads(line)
        for line in trajectory_path.read_text(encoding="utf-8").splitlines()
    ]
    judge_step = next(step for step in steps if step["status"] == "JUDGE_SCORED")
    assert judge_step["metadata"]["graph"]["backend"] == "langgraph"
    assert judge_step["metadata"]["graph"]["node_trace"] == [
        "PREPARE",
        "MINIMIZER",
        "AST_GUARD",
        "PYTEST",
        "MINIMIZER",
        "AST_GUARD",
        "PYTEST",
        "ADVERSARY",
        "MUTATION",
        "JUDGE",
    ]
    assert result.graph_node_trace == judge_step["metadata"]["graph"]["node_trace"] + ["FINALIZE"]
    assert judge_step["metadata"]["graph"]["verdict"] == "APPROVE"
    assert (result.workspace_path / "leap_year.py").read_text(encoding="utf-8") != (
        project / "leap_year.py"
    ).read_text(encoding="utf-8")


def test_orchestrator_injects_and_persists_trajectory_memory(tmp_path: Path):
    project = _make_leap_project(tmp_path)
    store = SQLiteRunStore(tmp_path / ".runs" / "runs.sqlite")
    store.save_memory(
        TrajectoryMemoryRecord(
            memory_id="memory-1",
            run_id="old-run",
            repo_name="leap",
            target_path="leap_year.py",
            status="FAILED",
            lesson="不要再把 1900 当成闰年。",
            error_signature="AssertionError: assert True is False",
        )
    )
    client = CapturingClient()
    request = RefactorRequest(
        target_file=project / "leap_year.py",
        issue_text="1900 should not be a leap year",
        tests_path=project / "tests",
        repo_name="leap",
        max_retry=1,
    )
    orchestrator = RefactorOrchestrator(
        llm_client=client,
        run_root=tmp_path / ".runs",
        store=store,
    )

    result = orchestrator.run(request)

    assert result.record.status == "SUCCESS"
    assert client.issue_text is not None
    assert "历史轨迹记忆" in client.issue_text
    assert "不要再把 1900 当成闰年" in client.issue_text
    memories = store.list_memory("leap", "leap_year.py", limit=5)
    assert any(memory.status == "SUCCESS" and memory.run_id == result.record.run_id for memory in memories)


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


def test_orchestrator_records_immediate_llm_failure_without_self_heal(tmp_path: Path):
    project = _make_leap_project(tmp_path)
    request = RefactorRequest(
        target_file=project / "leap_year.py",
        issue_text="1900 should not be a leap year",
        tests_path=project / "tests",
        repo_name="leap",
        max_retry=2,
    )
    result = RefactorOrchestrator(
        llm_client=LLMFailingClient(),
        run_root=tmp_path / ".runs",
        store=SQLiteRunStore(tmp_path / ".runs" / "runs.sqlite"),
    ).run(request)

    assert result.record.status == "FAILED"
    assert result.attempts == 1
    assert result.record.self_heal_count == 0


def test_orchestrator_supports_loop_graph_backend(tmp_path: Path):
    project = _make_leap_project(tmp_path)
    request = RefactorRequest(
        target_file=project / "leap_year.py",
        issue_text="1900 should not be a leap year",
        tests_path=project / "tests",
        repo_name="leap-loop",
        max_retry=2,
    )
    orchestrator = RefactorOrchestrator(
        llm_client=MockRefactorClient(),
        run_root=tmp_path / ".runs",
        store=SQLiteRunStore(tmp_path / ".runs" / "runs.sqlite"),
        graph_backend="loop",
    )
    result = orchestrator.run(request)
    assert result.record.status == "SUCCESS"
    trajectory_path = tmp_path / ".runs" / result.record.run_id / "trajectory.jsonl"
    steps = [json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines()]
    judge_step = next(step for step in steps if step["status"] == "JUDGE_SCORED")
    assert judge_step["metadata"]["graph"]["backend"] == "loop"
    assert judge_step["metadata"]["graph"]["verdict"] == "APPROVE"


def test_orchestrator_self_heals_after_adversary_counterexample(tmp_path: Path):
    project = _make_weak_business_day_project(tmp_path)
    request = RefactorRequest(
        target_file=project / "calendar_rules.py",
        issue_text="weekends and invalid values should not be business days",
        tests_path=project / "tests",
        repo_name="calendar",
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
    assert len(result.debate_rounds) == 2
    assert result.debate_rounds[0].pytest_passed is True
    assert result.debate_rounds[0].adversarial_passed is False
    assert result.debate_rounds[1].converged is True
    assert result.mutation_result is not None
    assert result.mutation_result.kill_rate == 1.0

    trajectory_path = tmp_path / ".runs" / result.record.run_id / "trajectory.jsonl"
    statuses = [
        json.loads(line)["status"]
        for line in trajectory_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "ADVERSARY_CRITIQUED" in statuses
    assert "ADVERSARY_FAILED" in statuses
    assert "DEBATE_CONVERGED" in statuses
    assert validate_status_sequence(statuses) == []


def test_orchestrator_fails_before_llm_when_docker_is_unavailable(
    tmp_path: Path,
    monkeypatch,
):
    project = _make_leap_project(tmp_path)
    request = RefactorRequest(
        target_file=project / "leap_year.py",
        issue_text="1900 should not be a leap year",
        tests_path=project / "tests",
        repo_name="leap",
        max_retry=2,
    )
    monkeypatch.setattr(
        "refactor_agent.sandbox.docker_status",
        lambda: DockerStatus(available=False, executable="docker", error="virtualization missing"),
    )
    orchestrator = RefactorOrchestrator(
        llm_client=ExplodingClient(),
        run_root=tmp_path / ".runs",
        store=SQLiteRunStore(tmp_path / ".runs" / "runs.sqlite"),
        sandbox_backend="docker",
    )
    result = orchestrator.run(request)
    assert result.record.status == "FAILED"
    assert result.attempts == 0
    assert result.record.self_heal_count == 0
    assert "virtualization missing" in (result.record.error or "")


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


def _make_weak_business_day_project(tmp_path: Path) -> Path:
    project = tmp_path / "calendar"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "calendar_rules.py").write_text(
        "def is_business_day(day):\n"
        "    if day == 1:\n"
        "        return True\n"
        "    if day == 2:\n"
        "        return True\n"
        "    if day == 3:\n"
        "        return True\n"
        "    if day == 4:\n"
        "        return True\n"
        "    if day == 5:\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    (tests / "test_calendar_rules.py").write_text(
        "from calendar_rules import is_business_day\n\n\n"
        "def test_weak_baseline():\n"
        "    assert is_business_day(1) is True\n"
        "    assert is_business_day(2) is True\n"
        "    assert is_business_day(0) is False\n",
        encoding="utf-8",
    )
    return project
