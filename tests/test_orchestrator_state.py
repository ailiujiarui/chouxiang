from refactor_agent.models import AgentDebateMessage, RewardBreakdown
from refactor_agent.orchestrator_state import (
    close_debate_round,
    initial_execution_state,
    retry_or_finalize,
    transition_to,
)


def test_initial_execution_state_is_fresh_for_each_run():
    first = initial_execution_state(3)
    second = initial_execution_state(3)

    assert first == {
        "attempt": 0,
        "max_attempts": 3,
        "current_code": "",
        "previous_error": None,
        "debate_rounds": [],
        "llm_usages": [],
        "node_trace": [],
        "next_node": "prepare",
    }
    first["debate_rounds"].append("changed")
    first["llm_usages"].append("changed")
    first["node_trace"].append("PREPARE")
    assert second["debate_rounds"] == []
    assert second["llm_usages"] == []
    assert second["node_trace"] == []


def test_transition_to_mutates_and_returns_same_state():
    state = initial_execution_state(2)

    returned = transition_to(state, "ast_guard")

    assert returned is state
    assert state["next_node"] == "ast_guard"


def test_retry_or_finalize_uses_attempt_limit_boundary():
    retry_state = initial_execution_state(3)
    retry_state["attempt"] = 2
    final_state = initial_execution_state(3)
    final_state["attempt"] = 3

    assert retry_or_finalize(retry_state)["next_node"] == "minimizer"
    assert retry_or_finalize(final_state)["next_node"] == "finalize"


def test_close_debate_round_builds_typed_round_with_updates():
    state = initial_execution_state(2)
    state["attempt"] = 1
    state["code_change_percent"] = 12.5
    state["round_messages"] = [
        AgentDebateMessage(round=1, agent="MINIMIZER", content="proposal")
    ]
    reward = RewardBreakdown(
        delta_loc=1,
        delta_cc=0,
        retry_count=0,
        mutation_kill_rate=1.0,
        adversarial_passed=True,
        reward=6.0,
    )

    close_debate_round(
        state,
        pytest_passed=True,
        adversarial_passed=True,
        mutation_kill_rate=1.0,
        reward=reward,
        converged=True,
    )

    assert len(state["debate_rounds"]) == 1
    round_result = state["debate_rounds"][0]
    assert round_result.round == 1
    assert round_result.code_change_percent == 12.5
    assert round_result.messages == state["round_messages"]
    assert round_result.pytest_passed is True
    assert round_result.adversarial_passed is True
    assert round_result.mutation_kill_rate == 1.0
    assert round_result.reward == reward
    assert round_result.converged is True
