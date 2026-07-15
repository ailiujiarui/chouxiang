from datetime import datetime, timedelta, timezone

from refactor_agent.execution_control import ExecutionControl
from refactor_agent.execution_graph import NODE_ORDER, run_execution_graph
import pytest


class RecordingNodes:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def prepare(self, state):
        return self._advance(state, "prepare", "minimizer")

    def minimizer(self, state):
        state["attempt"] = state.get("attempt", 0) + 1
        return self._advance(state, "minimizer", "ast_guard")

    def ast_guard(self, state):
        next_node = "minimizer" if state["attempt"] == 1 else "pytest"
        return self._advance(state, "ast_guard", next_node)

    def pytest(self, state):
        return self._advance(state, "pytest", "adversary")

    def adversary(self, state):
        return self._advance(state, "adversary", "mutation")

    def mutation(self, state):
        return self._advance(state, "mutation", "judge")

    def judge(self, state):
        return self._advance(state, "judge", "finalize")

    def finalize(self, state):
        return self._advance(state, "finalize", "finalize")

    def _advance(self, state, current, next_node):
        self.calls.append(current)
        state["next_node"] = next_node
        return state


def test_langgraph_executes_real_nodes_and_retry_edges():
    nodes = RecordingNodes()

    result = run_execution_graph({"node_trace": []}, nodes, "langgraph")

    assert nodes.calls == [
        "prepare",
        "minimizer",
        "ast_guard",
        "minimizer",
        "ast_guard",
        "pytest",
        "adversary",
        "mutation",
        "judge",
        "finalize",
    ]
    assert result["node_trace"] == [name.upper() for name in nodes.calls]


def test_loop_and_langgraph_execute_identical_node_contracts():
    results = {}
    for backend in ("langgraph", "loop"):
        nodes = RecordingNodes()
        results[backend] = run_execution_graph({"node_trace": []}, nodes, backend)
        assert nodes.calls[-1] == NODE_ORDER[-1]

    assert results["loop"] == results["langgraph"]


@pytest.mark.parametrize("backend", ["langgraph", "loop"])
def test_execution_graph_rejects_illegal_node_transition(backend):
    nodes = RecordingNodes()
    nodes.prepare = lambda state: {**state, "next_node": "pytest"}

    with pytest.raises(ValueError, match="Illegal execution graph transition: prepare -> pytest"):
        run_execution_graph({"node_trace": []}, nodes, backend)


@pytest.mark.parametrize("backend", ["langgraph", "loop"])
def test_execution_graph_stops_at_node_boundary_when_cancelled(backend):
    nodes = RecordingNodes()
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 2

    control = ExecutionControl(
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        is_cancel_requested=cancelled,
    )

    result = run_execution_graph({"node_trace": []}, nodes, backend, execution_control=control)

    assert nodes.calls == ["prepare", "finalize"]
    assert result["node_trace"] == ["PREPARE", "FINALIZE"]
    assert result["control_status"] == "CANCELLED"
    assert "after-prepare" in result["terminal_error"]


@pytest.mark.parametrize("backend", ["langgraph", "loop"])
def test_execution_graph_skips_first_node_after_deadline(backend):
    nodes = RecordingNodes()
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    control = ExecutionControl(deadline_at=now, clock=lambda: now)

    result = run_execution_graph({"node_trace": []}, nodes, backend, execution_control=control)

    assert nodes.calls == ["finalize"]
    assert result["node_trace"] == ["FINALIZE"]
    assert result["control_status"] == "TIMED_OUT"
