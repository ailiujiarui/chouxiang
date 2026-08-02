from __future__ import annotations

from typing import Protocol

from refactor_agent.execution_graph import ExecutionState
from refactor_agent.models import (
    AdversarialTestResult,
    AgentDebateMessage,
    MetricsSnapshot,
    MutationTestResult,
    RewardBreakdown,
)
from refactor_agent.orchestrator_state import (
    close_debate_round,
    retry_or_finalize,
    transition_to,
)


class CandidateJudge(Protocol):
    def score(
        self,
        pre: MetricsSnapshot,
        post: MetricsSnapshot,
        retry_count: int,
        mutation_result: MutationTestResult | None,
        adversarial_result: AdversarialTestResult | None = None,
    ) -> RewardBreakdown: ...


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


def run_judge_execution_node(
    state: ExecutionState,
    *,
    graph_backend: str,
    judge: CandidateJudge,
    record_trajectory: TrajectoryRecorder,
) -> ExecutionState:
    """Score the candidate, close its debate round, and choose the next node."""
    reward = judge.score(
        pre=state["baseline"],
        post=state["post"],
        retry_count=state["attempt"] - 1,
        mutation_result=state["mutation"],
        adversarial_result=state["adversarial"],
    )
    state["reward"] = reward
    approved = state["adversarial"].passed and state["mutation"].kill_rate >= 1.0
    verdict = (
        "APPROVE"
        if approved
        else ("RETRY" if state["attempt"] < state["max_attempts"] else "REJECT")
    )
    message = summarize_judge(reward)
    graph = {
        "backend": graph_backend,
        "node_trace": [*state.get("node_trace", []), "JUDGE"],
        "verdict": verdict,
    }
    state["round_messages"].append(
        AgentDebateMessage(
            round=state["attempt"],
            agent="JUDGE",
            content=message,
            metadata={"graph": graph},
        )
    )
    close_debate_round(
        state,
        pytest_passed=True,
        adversarial_passed=state["adversarial"].passed,
        mutation_kill_rate=state["mutation"].kill_rate,
        reward=reward,
        converged=approved,
    )
    record_trajectory(
        state,
        "JUDGE_SCORED",
        message,
        "JUDGE",
        {"graph": graph},
        reward,
    )
    if approved:
        state["approved"] = True
        record_trajectory(
            state,
            "DEBATE_CONVERGED",
            "Candidate passed the executed graph.",
            "JUDGE",
            reward=reward,
        )
        return transition_to(state, "finalize")
    survivors = "; ".join(state["mutation"].survival_details) or "none"
    state["previous_error"] = (
        f"Judge verdict: {verdict}. "
        f"Mutation kill rate: {state['mutation'].kill_rate:.3f}. "
        f"Surviving mutants: {survivors}"
    )
    return retry_or_finalize(state)


def summarize_judge(reward: RewardBreakdown) -> str:
    return (
        "裁判评分="
        f"{reward.reward:.2f}；LOC 改善={reward.delta_loc}；圈复杂度改善={reward.delta_cc}；"
        f"变异击杀率={reward.mutation_kill_rate:.2f}；重试次数={reward.retry_count}。"
    )
