import pytest

from refactor_agent.debate_graph import run_debate_graph


@pytest.mark.parametrize("backend", ["langgraph", "loop"])
def test_debate_graph_approves_complete_validation(backend):
    state = run_debate_graph(
        attempt=1,
        max_attempts=3,
        ast_ok=True,
        pytest_passed=True,
        adversarial_passed=True,
        mutation_kill_rate=1.0,
        reward=42.0,
        failure_feedback=None,
        backend=backend,
    )
    assert state["node_trace"] == ["MINIMIZER", "DEFENDER", "ADVERSARY", "JUDGE"]
    assert state["verdict"] == "APPROVE"


@pytest.mark.parametrize("backend", ["langgraph", "loop"])
def test_debate_graph_retries_then_rejects(backend):
    retry = run_debate_graph(
        attempt=1,
        max_attempts=2,
        ast_ok=False,
        pytest_passed=False,
        adversarial_passed=None,
        mutation_kill_rate=None,
        reward=None,
        failure_feedback="AST rejected",
        backend=backend,
    )
    rejected = run_debate_graph(
        attempt=2,
        max_attempts=2,
        ast_ok=True,
        pytest_passed=True,
        adversarial_passed=False,
        mutation_kill_rate=0.5,
        reward=-10.0,
        failure_feedback="counterexample",
        backend=backend,
    )
    assert retry["node_trace"] == ["MINIMIZER", "DEFENDER", "JUDGE"]
    assert retry["verdict"] == "RETRY"
    assert rejected["node_trace"] == ["MINIMIZER", "DEFENDER", "ADVERSARY", "JUDGE"]
    assert rejected["verdict"] == "REJECT"


def test_debate_graph_rejects_unknown_backend():
    with pytest.raises(ValueError, match="Unsupported graph backend"):
        run_debate_graph(
            attempt=1,
            max_attempts=1,
            ast_ok=False,
            pytest_passed=False,
            adversarial_passed=None,
            mutation_kill_rate=None,
            reward=None,
            failure_feedback=None,
            backend="other",
        )
