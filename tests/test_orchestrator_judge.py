from pathlib import Path

import pytest

from refactor_agent.models import (
    AdversarialTestResult,
    MetricsSnapshot,
    MutationTestResult,
    RewardBreakdown,
)
from refactor_agent.orchestrator import _summarize_judge
from refactor_agent.orchestrator_judge import (
    run_judge_execution_node,
    summarize_judge,
)
from refactor_agent.orchestrator_state import initial_execution_state


class Judge:
    def __init__(self, reward: RewardBreakdown) -> None:
        self.reward = reward
        self.calls: list[dict] = []

    def score(self, **kwargs) -> RewardBreakdown:
        self.calls.append(kwargs)
        return self.reward


def test_judge_node_approves_and_closes_round(tmp_path: Path):
    state = _state(tmp_path, attempt=1, max_attempts=2, adversarial_passed=True)
    judge = Judge(_reward())
    trajectories: list[tuple[tuple, dict]] = []

    returned = run_judge_execution_node(
        state,
        graph_backend="langgraph",
        judge=judge,
        record_trajectory=lambda *args, **kwargs: trajectories.append((args, kwargs)),
    )

    assert returned is state
    assert state["approved"] is True
    assert state["next_node"] == "finalize"
    assert state["reward"] == judge.reward
    assert judge.calls == [
        {
            "pre": state["baseline"],
            "post": state["post"],
            "retry_count": 0,
            "mutation_result": state["mutation"],
            "adversarial_result": state["adversarial"],
        }
    ]
    graph = state["round_messages"][-1].metadata["graph"]
    assert graph == {
        "backend": "langgraph",
        "node_trace": ["PREPARE", "JUDGE"],
        "verdict": "APPROVE",
    }
    assert state["debate_rounds"][-1].converged is True
    assert state["debate_rounds"][-1].reward == judge.reward
    assert [call[0][1] for call in trajectories] == [
        "JUDGE_SCORED",
        "DEBATE_CONVERGED",
    ]
    assert trajectories[0][0][4] == {"graph": graph}
    assert trajectories[1][1]["reward"] == judge.reward


@pytest.mark.parametrize(
    ("attempt", "max_attempts", "expected_node", "expected_verdict"),
    [(1, 2, "minimizer", "RETRY"), (2, 2, "finalize", "REJECT")],
)
def test_judge_node_routes_failed_candidate(
    tmp_path: Path,
    attempt: int,
    max_attempts: int,
    expected_node: str,
    expected_verdict: str,
):
    state = _state(
        tmp_path,
        attempt=attempt,
        max_attempts=max_attempts,
        adversarial_passed=False,
    )
    state["mutation"] = MutationTestResult(
        total=2,
        killed=1,
        survived=1,
        survival_details=["replace return value"],
    )

    run_judge_execution_node(
        state,
        graph_backend="loop",
        judge=Judge(_reward(kill_rate=0.5)),
        record_trajectory=lambda *args, **kwargs: None,
    )

    assert state["next_node"] == expected_node
    assert state["round_messages"][-1].metadata["graph"]["verdict"] == expected_verdict
    assert state["debate_rounds"][-1].converged is False
    assert "Surviving mutants: replace return value" in state["previous_error"]


def test_judge_summary_keeps_compatibility_export():
    reward = _reward()

    assert summarize_judge(reward) == _summarize_judge(reward)
    assert "裁判评分=3.50" in summarize_judge(reward)


def _state(
    tmp_path: Path,
    *,
    attempt: int,
    max_attempts: int,
    adversarial_passed: bool,
):
    state = initial_execution_state(max_attempts)
    state.update(
        {
            "attempt": attempt,
            "baseline": MetricsSnapshot(loc=8, cyclomatic_complexity=3),
            "post": MetricsSnapshot(loc=6, cyclomatic_complexity=2),
            "mutation": MutationTestResult(total=2, killed=2, survived=0),
            "adversarial": AdversarialTestResult(
                generated=1,
                passed=adversarial_passed,
                returncode=0 if adversarial_passed else 1,
            ),
            "round_messages": [],
            "node_trace": ["PREPARE"],
        }
    )
    return state


def _reward(kill_rate: float = 1.0) -> RewardBreakdown:
    return RewardBreakdown(
        delta_loc=2,
        delta_cc=1,
        retry_count=0,
        mutation_kill_rate=kill_rate,
        adversarial_passed=kill_rate >= 1.0,
        reward=3.5,
    )
