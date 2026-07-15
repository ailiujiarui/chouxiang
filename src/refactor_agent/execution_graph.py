from __future__ import annotations

from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph


class RefactorGraphState(TypedDict, total=False):
    active_backend: str
    adversarial: Any
    allowed_regions: list[Any]
    attempt: int
    baseline: Any
    code_change_percent: float
    debate_rounds: list[Any]
    llm_request: Any
    llm_result: Any
    max_attempts: int
    mutation: Any
    current_code: str
    original_code: str
    performance: Any
    post: Any
    previous_candidate_code: str
    previous_error: str | None
    reward: Any
    rewrite: Any
    round_messages: list[Any]
    sandbox: Any
    target_file: Any
    terminal_error: str
    tests_path: Any
    validation: Any
    next_node: str
    node_trace: list[str]
    approved: bool
    result: Any


ExecutionState = RefactorGraphState


class ExecutionNodes(Protocol):
    def prepare(self, state: ExecutionState) -> ExecutionState: ...
    def minimizer(self, state: ExecutionState) -> ExecutionState: ...
    def ast_guard(self, state: ExecutionState) -> ExecutionState: ...
    def pytest(self, state: ExecutionState) -> ExecutionState: ...
    def adversary(self, state: ExecutionState) -> ExecutionState: ...
    def mutation(self, state: ExecutionState) -> ExecutionState: ...
    def judge(self, state: ExecutionState) -> ExecutionState: ...
    def finalize(self, state: ExecutionState) -> ExecutionState: ...


NODE_ORDER = ("prepare", "minimizer", "ast_guard", "pytest", "adversary", "mutation", "judge", "finalize")
NODE_ROUTES = {
    "prepare": ("minimizer", "finalize"),
    "minimizer": ("ast_guard", "finalize"),
    "ast_guard": ("pytest", "minimizer", "finalize"),
    "pytest": ("adversary", "minimizer", "finalize"),
    "adversary": ("mutation", "minimizer", "finalize"),
    "mutation": ("judge", "finalize"),
    "judge": ("minimizer", "finalize"),
}


def run_execution_graph(initial: ExecutionState, nodes: ExecutionNodes, backend: str) -> ExecutionState:
    if backend == "loop":
        return _run_loop(initial, nodes)
    if backend != "langgraph":
        raise ValueError(f"Unsupported graph backend: {backend}")
    graph = StateGraph(RefactorGraphState)
    for name in NODE_ORDER:
        graph.add_node(name, _wrapped(name, getattr(nodes, name)))
    graph.add_edge(START, "prepare")
    for name in NODE_ORDER[:-1]:
        graph.add_conditional_edges(name, _route_from(name), {target: target for target in NODE_ROUTES[name]})
    graph.add_edge("finalize", END)
    return graph.compile().invoke(initial)


def _run_loop(initial: ExecutionState, nodes: ExecutionNodes) -> ExecutionState:
    state = initial
    current = "prepare"
    while True:
        state = _wrapped(current, getattr(nodes, current))(state)
        if current == "finalize":
            return state
        current = _next_node(current, state)


def _wrapped(name: str, node):
    def invoke(state: ExecutionState) -> ExecutionState:
        updated = node(dict(state))
        updated["node_trace"] = [*state.get("node_trace", []), name.upper()]
        return updated

    return invoke


def _route_from(current: str):
    def route(state: ExecutionState) -> str:
        return _next_node(current, state)

    return route


def _next_node(current: str, state: ExecutionState) -> str:
    target = str(state.get("next_node") or "finalize")
    if target not in NODE_ROUTES[current]:
        raise ValueError(f"Illegal execution graph transition: {current} -> {target}")
    return target


def render_execution_mermaid() -> str:
    lines = ["stateDiagram-v2", "    [*] --> PREPARE"]
    for source, targets in NODE_ROUTES.items():
        for target in targets:
            lines.append(f"    {source.upper()} --> {target.upper()}")
    lines.append("    FINALIZE --> [*]")
    return "\n".join(lines)
