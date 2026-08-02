from datetime import datetime, timedelta, timezone
from pathlib import Path

from refactor_agent.execution_control import ExecutionControl
from refactor_agent.models import (
    AdversarialTestResult,
    AgentDebateMessage,
    AstRewriteResult,
    MetricsSnapshot,
    MutationTestResult,
    PerformanceProfile,
)
from refactor_agent.orchestrator import (
    _combined_mutation_tests_path,
    _summarize_mutation,
)
from refactor_agent.orchestrator_mutation import (
    combined_mutation_tests_path,
    run_mutation_execution_node,
    summarize_mutation,
)
from refactor_agent.orchestrator_state import initial_execution_state


class Adversary:
    def __init__(self, result: MutationTestResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def challenge(self, **kwargs) -> MutationTestResult:
        self.calls.append(kwargs)
        return self.result


def test_mutation_node_collects_metrics_mutation_and_performance(
    monkeypatch,
    tmp_path: Path,
):
    state = _state(tmp_path)
    mutation = MutationTestResult(total=2, killed=2, survived=0)
    performance = PerformanceProfile(
        passed=True,
        pytest_returncode=0,
        pytest_duration_seconds=0.2,
        peak_memory_kib=128.0,
    )
    post = MetricsSnapshot(loc=2, cyclomatic_complexity=1)
    adversary = Adversary(mutation)
    profiles: list[dict] = []
    monkeypatch.setattr(
        "refactor_agent.orchestrator_mutation.analyze_file",
        lambda path: post,
    )
    monkeypatch.setattr(
        "refactor_agent.orchestrator_mutation.run_performance_profile_with_backend",
        lambda **kwargs: profiles.append(kwargs) or performance,
    )
    control = _control()
    trajectory: list[tuple] = []

    returned = run_mutation_execution_node(
        state,
        workspace=tmp_path / "workspace",
        timeout_seconds=19.0,
        docker_image="sandbox:test",
        docker_memory="512m",
        docker_cpus=2.0,
        execution_control=control,
        adversary=adversary,
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert returned is state
    assert state["post"] == post
    assert state["mutation"] == mutation
    assert state["performance"] == performance
    assert state["next_node"] == "judge"
    assert state["round_messages"][-1] == AgentDebateMessage(
        round=1,
        agent="ADVERSARY",
        content="变异攻击击杀 2/2 个变异体（击杀率 100.0%）。",
    )
    assert trajectory == [
        (
            state,
            "ADVERSARY_CHALLENGED",
            state["round_messages"][-1].content,
            "ADVERSARY",
        )
    ]
    assert adversary.calls[0]["target_regions"] == ["value"]
    assert adversary.calls[0]["tests_path"] == state["tests_path"]
    assert adversary.calls[0]["execution_control"] is control
    assert profiles == [
        {
            "workspace": tmp_path / "workspace",
            "target_file": state["target_file"],
            "tests_path": state["tests_path"],
            "timeout_seconds": 19.0,
            "backend": "subprocess",
            "docker_image": "sandbox:test",
            "memory": "512m",
            "cpus": 2.0,
            "execution_control": control,
        }
    ]


def test_combined_mutation_tests_replaces_stale_directory_and_copies_inputs(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    baseline = tmp_path / "tests"
    baseline.mkdir()
    (baseline / "test_base.py").write_text("def test_base(): pass\n", encoding="utf-8")
    adversarial = tmp_path / "test_adversarial.py"
    adversarial.write_text("def test_edge(): pass\n", encoding="utf-8")
    stale = workspace / "_mutation_tests"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("stale", encoding="utf-8")

    combined = combined_mutation_tests_path(workspace, baseline, adversarial)

    assert combined == workspace / "_mutation_tests"
    assert not (combined / "stale.txt").exists()
    assert (combined / "baseline" / "test_base.py").is_file()
    assert (combined / "adversarial" / "test_adversarial.py").is_file()
    assert _combined_mutation_tests_path(workspace, baseline, None) == baseline


def test_mutation_summary_keeps_compatibility_export():
    result = MutationTestResult(total=4, killed=3, survived=1)

    assert "75.0%" in summarize_mutation(result)
    assert _summarize_mutation(result) == summarize_mutation(result)


def _state(tmp_path: Path):
    tests = tmp_path / "workspace" / "tests"
    tests.mkdir(parents=True)
    target = tmp_path / "workspace" / "module.py"
    target.write_text("def value(): return 1\n", encoding="utf-8")
    state = initial_execution_state(2)
    state.update(
        {
            "attempt": 1,
            "current_code": "def value(): return 1\n",
            "target_file": target,
            "tests_path": tests,
            "active_backend": "subprocess",
            "adversarial": AdversarialTestResult(
                generated=0,
                passed=True,
                returncode=0,
            ),
            "rewrite": AstRewriteResult(
                ok=True,
                source="def value(): return 1\n",
                allowed_regions=["value"],
                changed_regions=["value"],
            ),
            "round_messages": [],
        }
    )
    return state


def _control() -> ExecutionControl:
    return ExecutionControl(
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)
    )
