from refactor_agent.errors import ErrorCode, public_error_message
from refactor_agent.llm import LLMError
from refactor_agent.models import (
    LLMRefactorResult,
    LLMUsage,
    MetricsSnapshot,
    RefactorRequest,
)
from refactor_agent.orchestrator_minimizer import minimize_execution_node
from refactor_agent.orchestrator_state import initial_execution_state


class CapturingMinimizer:
    def __init__(self, result: LLMRefactorResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def propose(self, **kwargs) -> LLMRefactorResult:
        self.calls.append(kwargs)
        return self.result


class FailingMinimizer:
    def propose(self, **kwargs) -> LLMRefactorResult:
        raise LLMError("provider secret")


def test_minimize_execution_node_records_candidate_and_usage(tmp_path, monkeypatch):
    request = _request(tmp_path)
    llm_request = request.model_copy(update={"issue_text": "request with memory"})
    previous_usage = LLMUsage(provider="deepseek", model="old", total_tokens=3)
    usage = LLMUsage(
        provider="deepseek",
        model="deepseek-chat",
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        cost_usd=0.001,
    )
    result = LLMRefactorResult(
        thought="replace the branch with one expression",
        fixed_code="def value(flag):\n    return int(flag)\n",
        insult_review="less ceremony",
        usage=usage,
    )
    minimizer = CapturingMinimizer(result)
    state = _state(llm_request)
    state["llm_usages"] = [previous_usage]
    selected_regions = ["selected-region"]
    monkeypatch.setattr(
        "refactor_agent.orchestrator_minimizer.select_target_regions",
        lambda original, issue, previous_error: selected_regions,
    )
    trajectory: list[tuple] = []

    returned = minimize_execution_node(
        state,
        request=request,
        minimizer=minimizer,
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert returned is state
    assert state["attempt"] == 1
    assert state["allowed_regions"] == selected_regions
    assert state["llm_result"] == result
    assert state["llm_usages"] == [previous_usage, usage]
    assert state["next_node"] == "ast_guard"
    assert len(state["round_messages"]) == 1
    assert state["round_messages"][0].round == 1
    assert state["round_messages"][0].agent == "MINIMIZER"
    assert state["round_messages"][0].content == result.thought
    assert trajectory == [
        (state, "MINIMIZER_PROPOSED", result.thought, "MINIMIZER")
    ]
    assert minimizer.calls == [
        {
            "request": llm_request,
            "current_code": state["current_code"],
            "baseline_metrics": state["baseline"],
            "previous_error": "previous failure",
            "attempt": 1,
        }
    ]


def test_minimize_execution_node_keeps_usage_list_when_result_has_no_usage(tmp_path):
    request = _request(tmp_path)
    state = _state(request)
    original_usages = state["llm_usages"]

    minimize_execution_node(
        state,
        request=request,
        minimizer=CapturingMinimizer(
            LLMRefactorResult(
                thought="candidate",
                fixed_code="def value(flag):\n    return int(flag)\n",
                insult_review="review",
            )
        ),
        record_trajectory=lambda *args: None,
    )

    assert state["llm_usages"] is original_usages
    assert state["llm_usages"] == []


def test_minimize_execution_node_routes_llm_error_to_finalize(tmp_path):
    request = _request(tmp_path)
    state = _state(request)
    trajectory: list[tuple] = []

    minimize_execution_node(
        state,
        request=request,
        minimizer=FailingMinimizer(),
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert state["attempt"] == 1
    assert state["next_node"] == "finalize"
    assert state["terminal_error"] == public_error_message(ErrorCode.INTERNAL_ERROR)
    assert state["terminal_error_code"] == ErrorCode.INTERNAL_ERROR
    assert state["terminal_error_summary"] == "provider secret"
    assert "provider secret" not in state["terminal_error"]
    assert "llm_result" not in state
    assert "round_messages" not in state
    assert trajectory == []


def _request(tmp_path) -> RefactorRequest:
    target = tmp_path / "module.py"
    tests = tmp_path / "tests"
    tests.mkdir()
    target.write_text("def value(flag):\n    if flag:\n        return 1\n    return 0\n", encoding="utf-8")
    return RefactorRequest(
        target_file=target,
        issue_text="Simplify value",
        tests_path=tests,
        max_retry=2,
    )


def _state(llm_request: RefactorRequest):
    state = initial_execution_state(2)
    state.update(
        {
            "llm_request": llm_request,
            "original_code": "def value(flag):\n    if flag:\n        return 1\n    return 0\n",
            "current_code": "def value(flag):\n    if flag:\n        return 1\n    return 0\n",
            "baseline": MetricsSnapshot(loc=4, cyclomatic_complexity=2),
            "previous_error": "previous failure",
        }
    )
    return state
