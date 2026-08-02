from __future__ import annotations

from difflib import SequenceMatcher
from typing import Protocol

from refactor_agent.analysis_events import AnalysisEventType, SafeMetric
from refactor_agent.ast_analyzer import controlled_subtree_rewrite, validate_candidate_source
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.models import (
    AstRewriteResult,
    CandidateValidationResult,
    AgentDebateMessage,
)
from refactor_agent.orchestrator_state import (
    close_debate_round,
    retry_or_finalize,
    transition_to,
)


class StaticDefender(Protocol):
    def review_static(self, validation: CandidateValidationResult) -> str: ...


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
        metadata: dict | None = None,
    ) -> None: ...


def guard_ast_execution_node(
    state: ExecutionState,
    *,
    allowed_import_roots: set[str],
    defender: StaticDefender,
    emit_analysis_event: AnalysisEventEmitter,
    record_trajectory: TrajectoryRecorder,
) -> ExecutionState:
    """Rewrite only selected AST regions and reject unsafe candidate changes."""
    rewrite = controlled_subtree_rewrite(
        state["original_code"],
        state["llm_result"].fixed_code,
        state["allowed_regions"],
        allowed_import_roots,
    )
    state["rewrite"] = rewrite
    candidate = rewrite.source
    state["code_change_percent"] = code_change_percent(
        state.get("previous_candidate_code") or state["original_code"],
        candidate,
    )
    state["previous_candidate_code"] = candidate
    state["current_code"] = candidate
    validation = validate_candidate_source(state["original_code"], candidate)
    if not rewrite.ok:
        validation = CandidateValidationResult(ok=False, findings=rewrite.findings)
    state["validation"] = validation
    message = defender.review_static(validation)
    state["round_messages"].append(
        AgentDebateMessage(
            round=state["attempt"],
            agent="DEFENDER",
            content=message,
        )
    )

    if not validation.ok:
        state["previous_error"] = "AST guard rejected candidate:\n" + validation.summary()
        emit_analysis_event(
            AnalysisEventType.AST_REJECTED,
            state,
            phase="ast_guard",
            error_category="ast_guard_rejected",
            recoverable=state["attempt"] < state["max_attempts"],
        )
        record_trajectory(
            state,
            "AST_REJECTED",
            state["previous_error"],
            "DEFENDER",
            rewrite_metadata(rewrite),
        )
        close_debate_round(state)
        return retry_or_finalize(state)

    record_trajectory(
        state,
        "DEFENDER_REVIEWED",
        message,
        "DEFENDER",
        rewrite_metadata(rewrite),
    )
    return transition_to(state, "pytest")


def rewrite_metadata(rewrite: AstRewriteResult) -> dict[str, object]:
    return {
        "selected_targets": [
            region.model_dump(mode="json") for region in rewrite.selected_regions
        ],
        "changed_regions": rewrite.changed_regions,
        "added_imports": rewrite.added_imports,
    }


def code_change_percent(before: str, after: str) -> float:
    return (1.0 - SequenceMatcher(None, before, after).ratio()) * 100
