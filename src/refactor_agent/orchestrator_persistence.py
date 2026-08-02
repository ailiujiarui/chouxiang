from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from refactor_agent.memory import failure_memory, success_memory
from refactor_agent.models import (
    EvidenceLevel,
    ReportPersona,
    RunRecord,
    TrajectoryMemoryRecord,
)


class RunOutcomeStore(Protocol):
    def save(self, record: RunRecord) -> None: ...

    def save_memory(self, record: TrajectoryMemoryRecord) -> None: ...


@dataclass(frozen=True)
class PersistedRunOutcome:
    record: RunRecord
    approved: bool
    error: str | None
    attempts: int


def persist_run_outcome(
    store: RunOutcomeStore,
    state: Mapping[str, Any],
    *,
    run_id: str,
    issue_id: str | None,
    repo_name: str,
    memory_key: str,
    evidence_level: EvidenceLevel,
    report_persona: ReportPersona,
) -> PersistedRunOutcome:
    """Persist the final run record and its success/failure trajectory memory."""
    baseline = state.get("baseline")
    approved = bool(state.get("approved"))
    error = None if approved else str(
        state.get("terminal_error") or state.get("previous_error") or "refactor failed"
    )
    error_code = state.get("terminal_error_code") if not approved else None
    error_message = state.get("terminal_error") if error_code else None
    error_summary = state.get("terminal_error_summary") if error_code else None
    post = state.get("post") if approved else None
    attempts = int(state.get("attempt", 0))
    if approved or state.get("terminal_error"):
        self_heal_count = max(attempts - 1, 0)
    else:
        self_heal_count = attempts

    record = RunRecord(
        run_id=run_id,
        issue_id=issue_id,
        repo_name=repo_name,
        pre_loc=baseline.loc if baseline else None,
        post_loc=post.loc if post else None,
        pre_cc=baseline.cyclomatic_complexity if baseline else None,
        post_cc=post.cyclomatic_complexity if post else None,
        self_heal_count=self_heal_count,
        status="SUCCESS" if approved else "FAILED",
        error=None,
        error_code=error_code,
        error_message=error_message,
        error_summary=error_summary,
        evidence_level=evidence_level,
        report_persona=report_persona,
    )
    store.save(record)

    if approved:
        llm_result = state.get("llm_result")
        store.save_memory(
            success_memory(
                record,
                memory_key,
                llm_result.insult_review,
                state["reward"],
            )
        )
    else:
        store.save_memory(failure_memory(record, memory_key))

    return PersistedRunOutcome(
        record=record,
        approved=approved,
        error=error,
        attempts=attempts,
    )
