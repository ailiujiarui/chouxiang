from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol

from refactor_agent.analysis_events import AnalysisEventType, SafeMetric
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.models import (
    EvidenceLevel,
    RefactorRunResult,
    ReportPersona,
    RewardBreakdown,
)
from refactor_agent.orchestrator_persistence import (
    RunOutcomeStore,
    persist_run_outcome,
)
from refactor_agent.orchestrator_state import transition_to


ReportBuilder = Callable[..., str]
ArtifactWriter = Callable[[ExecutionState, str], None]


class TrajectoryRecorder(Protocol):
    def __call__(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
        metadata: dict | None = None,
        reward: RewardBreakdown | None = None,
    ) -> None: ...


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


def run_finalize_execution_node(
    state: ExecutionState,
    *,
    store: RunOutcomeStore,
    workspace: Path,
    run_id: str,
    issue_id: str | None,
    repo_name: str,
    memory_key: str,
    evidence_level: EvidenceLevel,
    report_persona: ReportPersona,
    graph_backend: str,
    build_report: ReportBuilder,
    write_artifacts: ArtifactWriter,
    record_trajectory: TrajectoryRecorder,
    emit_analysis_event: AnalysisEventEmitter,
) -> ExecutionState:
    """Persist and assemble the terminal result for one execution graph."""
    graph_trace = [*state.get("node_trace", []), "FINALIZE"]
    outcome = persist_run_outcome(
        store,
        state,
        run_id=run_id,
        issue_id=issue_id,
        repo_name=repo_name,
        memory_key=memory_key,
        evidence_level=evidence_level,
        report_persona=report_persona,
    )
    record = outcome.record
    approved = outcome.approved
    error = outcome.error
    llm_result = state.get("llm_result")
    if not approved:
        record_trajectory(
            state,
            str(state.get("control_status") or "FAILED"),
            error or "refactor failed",
        )
    report = build_report(
        record,
        workspace,
        llm_result.insult_review if approved and llm_result else None,
        state.get("sandbox"),
        error,
        state.get("validation"),
        state.get("adversarial"),
        state.get("mutation"),
        state.get("reward"),
        state.get("performance"),
        state["debate_rounds"],
        state.get("rewrite"),
        graph_backend,
        graph_trace,
        evidence_level,
        report_persona,
        llm_usages=state.get("llm_usages", []),
    )
    write_artifacts(state, report)
    state["result"] = RefactorRunResult(
        record=record,
        report_markdown=report,
        workspace_path=workspace,
        attempts=outcome.attempts,
        last_sandbox_result=state.get("sandbox"),
        candidate_file=state.get("target_file"),
        ast_validation=state.get("validation"),
        ast_rewrite=state.get("rewrite"),
        adversarial_result=state.get("adversarial"),
        mutation_result=state.get("mutation"),
        performance_profile=state.get("performance"),
        debate_rounds=state["debate_rounds"],
        graph_backend=graph_backend,
        graph_node_trace=graph_trace,
        llm_usages=state.get("llm_usages", []),
        evidence_level=evidence_level,
        report_persona=report_persona,
    )
    reward = state.get("reward")
    mutation = state.get("mutation")
    emit_analysis_event(
        AnalysisEventType.FINAL_VERDICT_PASSED
        if approved
        else AnalysisEventType.FINAL_VERDICT_FAILED,
        state,
        phase="finalize",
        error_category=(
            None
            if approved
            else str(state.get("control_status") or "analysis_failed").casefold()
        ),
        recoverable=False,
        safe_metrics={
            "pre_loc": record.pre_loc,
            "post_loc": record.post_loc,
            "pre_cc": record.pre_cc,
            "post_cc": record.post_cc,
            "self_heal_count": record.self_heal_count,
            "reward": getattr(reward, "reward", None),
            "mutation_kill_rate": getattr(mutation, "kill_rate", None),
        },
    )
    return transition_to(state, "finalize")
