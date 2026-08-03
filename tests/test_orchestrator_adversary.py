from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from refactor_agent.analysis_events import AnalysisEventType
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.models import (
    AdversarialCritique,
    AdversarialTestResult,
    AgentDebateMessage,
)
from refactor_agent.orchestrator import (
    _summarize_adversarial_failure,
    _summarize_adversary_pass,
    _summarize_critique,
)
from refactor_agent.orchestrator_adversary import (
    run_adversary_execution_node,
    summarize_adversarial_failure,
    summarize_adversary_pass,
    summarize_critique,
)
from refactor_agent.orchestrator_state import initial_execution_state


class Adversary:
    def __init__(self, result: AdversarialTestResult) -> None:
        self.result = result
        self.generate_calls: list[dict] = []

    def critique(self, candidate_source: str, issue_text: str) -> AdversarialCritique:
        return AdversarialCritique(
            risk_level="MEDIUM",
            attack_plan=["zero", "negative"],
            counterexample_hint="try -1",
            rationale="boundary risk",
        )

    def generate_tests(self, **kwargs) -> AdversarialTestResult:
        self.generate_calls.append(kwargs)
        return self.result


def test_adversary_node_routes_pass_and_forwards_sandbox_config(tmp_path: Path):
    state = _state(tmp_path, max_attempts=2)
    result = _result(passed=True, generated=2, returncode=0)
    adversary = Adversary(result)
    events: list[tuple] = []
    trajectory: list[tuple] = []
    control = _control()

    returned = run_adversary_execution_node(
        state,
        issue_text="handle boundaries",
        workspace=tmp_path / "workspace",
        timeout_seconds=13.0,
        docker_image="sandbox:test",
        docker_memory="384m",
        docker_cpus=1.5,
        execution_control=control,
        adversary=adversary,
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert returned is state
    assert state["adversarial"] == result
    assert state["next_node"] == "mutation"
    assert [message.agent for message in state["round_messages"][-2:]] == [
        "ADVERSARY",
        "ADVERSARY",
    ]
    assert [step[1] for step in trajectory] == [
        "ADVERSARY_CRITIQUED",
        "ADVERSARY_CHALLENGED",
    ]
    assert events == [
        (
            (AnalysisEventType.ADVERSARY_PASSED, state),
            {"phase": "adversary", "safe_metrics": {"generated_tests": 2}},
        )
    ]
    assert adversary.generate_calls == [
        {
            "candidate_source": state["current_code"],
            "workspace": tmp_path / "workspace",
            "target_file": state["target_file"],
            "issue_text": "handle boundaries",
            "timeout_seconds": 13.0,
            "backend": "docker",
            "docker_image": "sandbox:test",
            "memory": "384m",
            "cpus": 1.5,
            "execution_control": control,
        }
    ]


@pytest.mark.parametrize(
    ("attempt", "expected_next", "recoverable"),
    [(1, "minimizer", True), (2, "finalize", False)],
)
def test_adversary_node_records_failure_and_closes_round(
    tmp_path: Path,
    attempt,
    expected_next,
    recoverable,
):
    state = _state(tmp_path, max_attempts=2)
    state["attempt"] = attempt
    result = _result(
        passed=False,
        generated=1,
        returncode=1,
        stdout="counterexample failed",
    )
    events: list[tuple] = []
    trajectory: list[tuple] = []

    run_adversary_execution_node(
        state,
        issue_text="handle boundaries",
        workspace=tmp_path / "workspace",
        timeout_seconds=10.0,
        docker_image="sandbox:test",
        docker_memory="256m",
        docker_cpus=1.0,
        execution_control=_control(),
        adversary=Adversary(result),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert state["next_node"] == expected_next
    assert "counterexample failed" in state["previous_error"]
    assert "红队风险=MEDIUM" in state["previous_error"]
    assert state["debate_rounds"][-1].pytest_passed is True
    assert state["debate_rounds"][-1].adversarial_passed is False
    assert [step[1] for step in trajectory] == [
        "ADVERSARY_CRITIQUED",
        "ADVERSARY_CHALLENGED",
        "ADVERSARY_FAILED",
    ]
    assert events == [
        (
            (AnalysisEventType.ADVERSARY_FAILED, state),
            {
                "phase": "adversary",
                "error_category": "adversary_failed",
                "recoverable": recoverable,
                "safe_metrics": {"generated_tests": 1},
            },
        )
    ]


def test_adversary_summaries_keep_compatibility_exports():
    critique = AdversarialCritique(
        risk_level="LOW",
        attack_plan=[],
        rationale="none",
    )
    empty = _result(passed=True, generated=0, returncode=0)
    failed = _result(passed=False, generated=1, returncode=7)

    assert "暂无命中规则" in summarize_critique(critique)
    assert _summarize_critique(critique) == summarize_critique(critique)
    assert "没找到" in summarize_adversary_pass(empty)
    assert _summarize_adversary_pass(empty) == summarize_adversary_pass(empty)
    assert "返回码 7" in summarize_adversarial_failure(failed)
    assert _summarize_adversarial_failure(failed) == summarize_adversarial_failure(failed)


def _state(tmp_path: Path, max_attempts: int):
    state = initial_execution_state(max_attempts)
    state.update(
        {
            "attempt": 1,
            "current_code": "def value(x):\n    return abs(x)\n",
            "target_file": tmp_path / "workspace" / "module.py",
            "active_backend": "docker",
            "round_messages": [
                AgentDebateMessage(round=1, agent="MINIMIZER", content="candidate")
            ],
        }
    )
    return state


def _result(
    *,
    passed: bool,
    generated: int,
    returncode: int,
    stdout: str = "",
) -> AdversarialTestResult:
    return AdversarialTestResult(
        generated=generated,
        passed=passed,
        returncode=returncode,
        stdout=stdout,
    )


def _control() -> ExecutionControl:
    return ExecutionControl(
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)
    )
