from __future__ import annotations

from typing import Any, Literal

from refactor_agent.execution_graph import ExecutionState
from refactor_agent.models import DebateRound


WorkflowNode = Literal[
    "prepare",
    "minimizer",
    "ast_guard",
    "pytest",
    "adversary",
    "mutation",
    "judge",
    "finalize",
]


def initial_execution_state(max_attempts: int) -> ExecutionState:
    """Create fresh mutable state for one orchestrator execution graph."""
    return {
        "attempt": 0,
        "max_attempts": max_attempts,
        "current_code": "",
        "previous_error": None,
        "debate_rounds": [],
        "llm_usages": [],
        "node_trace": [],
        "next_node": "prepare",
    }


def transition_to(state: ExecutionState, target: WorkflowNode) -> ExecutionState:
    state["next_node"] = target
    return state


def retry_or_finalize(state: ExecutionState) -> ExecutionState:
    target: WorkflowNode = (
        "minimizer" if state["attempt"] < state["max_attempts"] else "finalize"
    )
    return transition_to(state, target)


def close_debate_round(state: ExecutionState, **updates: Any) -> None:
    state["debate_rounds"].append(
        DebateRound(
            round=state["attempt"],
            code_change_percent=state.get("code_change_percent"),
            messages=state.get("round_messages", []),
            **updates,
        )
    )
