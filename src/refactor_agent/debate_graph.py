from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


GraphVerdict = Literal["APPROVE", "RETRY", "REJECT"]


class DebateGraphState(TypedDict):
    attempt: int
    max_attempts: int
    ast_ok: bool
    pytest_passed: bool
    adversarial_passed: bool | None
    mutation_kill_rate: float | None
    reward: float | None
    failure_feedback: str | None
    node_trace: list[str]
    verdict: GraphVerdict | None


def run_debate_graph(
    *,
    attempt: int,
    max_attempts: int,
    ast_ok: bool,
    pytest_passed: bool,
    adversarial_passed: bool | None,
    mutation_kill_rate: float | None,
    reward: float | None,
    failure_feedback: str | None,
    backend: str = "langgraph",
) -> DebateGraphState:
    state: DebateGraphState = {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "ast_ok": ast_ok,
        "pytest_passed": pytest_passed,
        "adversarial_passed": adversarial_passed,
        "mutation_kill_rate": mutation_kill_rate,
        "reward": reward,
        "failure_feedback": failure_feedback,
        "node_trace": [],
        "verdict": None,
    }
    if backend == "loop":
        for node in ("MINIMIZER", "DEFENDER"):
            state = _visit(state, node)
        if ast_ok and pytest_passed:
            state = _visit(state, "ADVERSARY")
        return _judge(state)
    if backend != "langgraph":
        raise ValueError(f"Unsupported graph backend: {backend}")
    return _compiled_graph().invoke(state)


def _compiled_graph():
    graph = StateGraph(DebateGraphState)
    graph.add_node("minimizer", lambda state: _visit(state, "MINIMIZER"))
    graph.add_node("defender", lambda state: _visit(state, "DEFENDER"))
    graph.add_node("adversary", lambda state: _visit(state, "ADVERSARY"))
    graph.add_node("judge", _judge)
    graph.add_edge(START, "minimizer")
    graph.add_edge("minimizer", "defender")
    graph.add_conditional_edges(
        "defender",
        lambda state: "adversary" if state["ast_ok"] and state["pytest_passed"] else "judge",
        {"adversary": "adversary", "judge": "judge"},
    )
    graph.add_edge("adversary", "judge")
    graph.add_edge("judge", END)
    return graph.compile()


def _visit(state: DebateGraphState, node: str) -> DebateGraphState:
    return {**state, "node_trace": [*state["node_trace"], node]}


def _judge(state: DebateGraphState) -> DebateGraphState:
    passed = (
        state["ast_ok"]
        and state["pytest_passed"]
        and state["adversarial_passed"] is not False
        and (state["mutation_kill_rate"] is None or state["mutation_kill_rate"] >= 1.0)
    )
    if passed:
        verdict: GraphVerdict = "APPROVE"
    elif state["attempt"] < state["max_attempts"]:
        verdict = "RETRY"
    else:
        verdict = "REJECT"
    return {
        **state,
        "node_trace": [*state["node_trace"], "JUDGE"],
        "verdict": verdict,
    }
