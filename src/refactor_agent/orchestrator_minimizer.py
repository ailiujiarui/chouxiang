from __future__ import annotations

from typing import Protocol

from refactor_agent.ast_analyzer import select_target_regions
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.llm import LLMError
from refactor_agent.models import (
    AgentDebateMessage,
    LLMRefactorResult,
    MetricsSnapshot,
    RefactorRequest,
)
from refactor_agent.orchestrator_state import transition_to


class Minimizer(Protocol):
    def propose(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult: ...


class TrajectoryRecorder(Protocol):
    def __call__(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
    ) -> None: ...


def minimize_execution_node(
    state: ExecutionState,
    *,
    request: RefactorRequest,
    minimizer: Minimizer,
    record_trajectory: TrajectoryRecorder,
) -> ExecutionState:
    """Select a constrained target and request one candidate from the minimizer."""
    state["attempt"] += 1
    state["allowed_regions"] = select_target_regions(
        state["original_code"],
        request.issue_text,
        state.get("previous_error"),
    )
    try:
        result = minimizer.propose(
            request=state["llm_request"],
            current_code=state["current_code"],
            baseline_metrics=state["baseline"],
            previous_error=state.get("previous_error"),
            attempt=state["attempt"],
        )
    except LLMError as exc:
        state["terminal_error"] = exc.public_message
        state["terminal_error_code"] = exc.code
        state["terminal_error_summary"] = exc.summary
        return transition_to(state, "finalize")

    state["llm_result"] = result
    if result.usage is not None:
        state["llm_usages"] = [*state.get("llm_usages", []), result.usage]
    state["round_messages"] = [
        AgentDebateMessage(
            round=state["attempt"],
            agent="MINIMIZER",
            content=result.thought,
        )
    ]
    record_trajectory(
        state,
        "MINIMIZER_PROPOSED",
        result.thought,
        "MINIMIZER",
    )
    return transition_to(state, "ast_guard")
