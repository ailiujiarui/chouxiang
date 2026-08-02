from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from refactor_agent.analysis_events import AnalysisEventType
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.models import AgentDebateMessage, SandboxResult
from refactor_agent.orchestrator import _summarize_failure
from refactor_agent.orchestrator_pytest import (
    run_pytest_execution_node,
    summarize_pytest_failure,
)
from refactor_agent.orchestrator_state import initial_execution_state


class Defender:
    def review_pytest(self, result: SandboxResult) -> str:
        return "tests passed" if result.passed else "tests failed"


def test_pytest_node_writes_candidate_and_routes_pass(monkeypatch, tmp_path: Path):
    state = _state(tmp_path, max_attempts=2)
    result = _result(passed=True, returncode=0)
    writes: list[tuple] = []
    runs: list[dict] = []
    events: list[tuple] = []
    monkeypatch.setattr(
        "refactor_agent.orchestrator_pytest.write_candidate",
        lambda *args: writes.append(args),
    )
    monkeypatch.setattr(
        "refactor_agent.orchestrator_pytest.run_pytest_with_backend",
        lambda **kwargs: runs.append(kwargs) or result,
    )
    control = _control()

    returned = run_pytest_execution_node(
        state,
        workspace=tmp_path / "workspace",
        timeout_seconds=17.0,
        docker_image="sandbox:test",
        docker_memory="384m",
        docker_cpus=1.5,
        execution_control=control,
        defender=Defender(),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: pytest.fail("pass path must not record failure trajectory"),
    )

    assert returned is state
    assert writes == [(state["target_file"], state["current_code"])]
    assert runs == [
        {
            "workspace": tmp_path / "workspace",
            "tests_path": state["tests_path"],
            "timeout_seconds": 17.0,
            "backend": "docker",
            "docker_image": "sandbox:test",
            "memory": "384m",
            "cpus": 1.5,
            "execution_control": control,
        }
    ]
    assert state["sandbox"] == result
    assert state["round_messages"][-1] == AgentDebateMessage(
        round=1,
        agent="DEFENDER",
        content="tests passed",
    )
    assert state["next_node"] == "adversary"
    assert events == [
        (
            (AnalysisEventType.PYTEST_PASSED, state),
            {
                "phase": "pytest",
                "safe_metrics": {"returncode": 0, "duration_seconds": 0.25},
            },
        )
    ]


@pytest.mark.parametrize(
    ("attempt", "expected_next", "recoverable"),
    [(1, "minimizer", True), (2, "finalize", False)],
)
def test_pytest_node_records_failure_and_closes_round(
    monkeypatch,
    tmp_path: Path,
    attempt,
    expected_next,
    recoverable,
):
    state = _state(tmp_path, max_attempts=2)
    state["attempt"] = attempt
    result = _result(
        passed=False,
        returncode=1,
        stdout="failure detail",
        stderr="assertion error",
    )
    monkeypatch.setattr(
        "refactor_agent.orchestrator_pytest.write_candidate",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "refactor_agent.orchestrator_pytest.run_pytest_with_backend",
        lambda **kwargs: result,
    )
    events: list[tuple] = []
    trajectory: list[tuple] = []

    run_pytest_execution_node(
        state,
        workspace=tmp_path / "workspace",
        timeout_seconds=10.0,
        docker_image="sandbox:test",
        docker_memory="256m",
        docker_cpus=1.0,
        execution_control=_control(),
        defender=Defender(),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert state["previous_error"] == "failure detail\nassertion error"
    assert state["next_node"] == expected_next
    assert state["debate_rounds"][-1].pytest_passed is False
    assert trajectory == [
        (state, "PYTEST_FAILED", state["previous_error"], "DEFENDER")
    ]
    assert events == [
        (
            (AnalysisEventType.PYTEST_FAILED, state),
            {
                "phase": "pytest",
                "error_category": "pytest_failed",
                "recoverable": recoverable,
                "safe_metrics": {"returncode": 1, "duration_seconds": 0.25},
            },
        )
    ]


def test_failure_summary_caps_output_and_keeps_compatibility_export():
    result = _result(passed=False, returncode=3, stdout="x" * 9000)

    summary = summarize_pytest_failure(result)

    assert len(summary) == 8000
    assert _summarize_failure(result) == summary
    assert summarize_pytest_failure(_result(passed=False, returncode=7)) == (
        "pytest 失败，返回码 7"
    )


def _state(tmp_path: Path, max_attempts: int):
    state = initial_execution_state(max_attempts)
    state.update(
        {
            "attempt": 1,
            "target_file": tmp_path / "workspace" / "module.py",
            "tests_path": tmp_path / "workspace" / "tests",
            "current_code": "def value():\n    return 1\n",
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
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> SandboxResult:
    return SandboxResult(
        passed=passed,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.25,
    )


def _control() -> ExecutionControl:
    return ExecutionControl(
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5)
    )
