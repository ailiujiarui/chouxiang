from __future__ import annotations

from pathlib import Path
from typing import Protocol

from refactor_agent.analysis_events import AnalysisEventType, SafeMetric
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.models import AgentDebateMessage, SandboxResult
from refactor_agent.orchestrator_state import (
    close_debate_round,
    retry_or_finalize,
    transition_to,
)
from refactor_agent.sandbox import run_pytest_with_backend, write_candidate


class PytestDefender(Protocol):
    def review_pytest(self, result: SandboxResult) -> str: ...


class AnalysisEventEmitter(Protocol):
    def __call__(
        self,
        event_type: AnalysisEventType,
        state: ExecutionState,
        *,
        phase: str | None = None,
        error_category: str | None = None,
        recoverable: bool | None = None,
        safe_metrics: dict[str, SafeMetric] | None = None,
    ) -> None: ...


class TrajectoryRecorder(Protocol):
    def __call__(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
    ) -> None: ...


def run_pytest_execution_node(
    state: ExecutionState,
    *,
    workspace: Path,
    timeout_seconds: float,
    docker_image: str,
    docker_memory: str,
    docker_cpus: float,
    execution_control: ExecutionControl,
    defender: PytestDefender,
    emit_analysis_event: AnalysisEventEmitter,
    record_trajectory: TrajectoryRecorder,
) -> ExecutionState:
    """Write the candidate, execute regression tests, and route their verdict."""
    write_candidate(state["target_file"], state["current_code"])
    result = run_pytest_with_backend(
        workspace=workspace,
        tests_path=state["tests_path"],
        timeout_seconds=timeout_seconds,
        backend=state["active_backend"],
        docker_image=docker_image,
        memory=docker_memory,
        cpus=docker_cpus,
        execution_control=execution_control,
    )
    state["sandbox"] = result
    message = defender.review_pytest(result)
    state["round_messages"].append(
        AgentDebateMessage(
            round=state["attempt"],
            agent="DEFENDER",
            content=message,
        )
    )

    safe_metrics = {
        "returncode": result.returncode,
        "duration_seconds": result.duration_seconds,
    }
    if not result.passed:
        state["previous_error"] = summarize_pytest_failure(result)
        emit_analysis_event(
            AnalysisEventType.PYTEST_FAILED,
            state,
            phase="pytest",
            error_category="pytest_failed",
            recoverable=state["attempt"] < state["max_attempts"],
            safe_metrics=safe_metrics,
        )
        record_trajectory(
            state,
            "PYTEST_FAILED",
            state["previous_error"],
            "DEFENDER",
        )
        close_debate_round(state, pytest_passed=False)
        return retry_or_finalize(state)

    emit_analysis_event(
        AnalysisEventType.PYTEST_PASSED,
        state,
        phase="pytest",
        safe_metrics=safe_metrics,
    )
    return transition_to(state, "adversary")


def summarize_pytest_failure(result: SandboxResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return combined[-8000:] if combined else f"pytest 失败，返回码 {result.returncode}"
