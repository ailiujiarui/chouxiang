from refactor_agent.debate_state import (
    render_mermaid_state_diagram,
    should_converge,
    validate_status_sequence,
)


def test_render_mermaid_state_diagram_contains_execution_nodes_and_retry():
    diagram = render_mermaid_state_diagram()
    assert "PREPARE --> MINIMIZER" in diagram
    assert "AST_GUARD --> PYTEST" in diagram
    assert "ADVERSARY --> MUTATION" in diagram
    assert "JUDGE --> MINIMIZER" in diagram
    assert "JUDGE --> FINALIZE" in diagram


def test_validate_status_sequence_accepts_counterexample_retry():
    errors = validate_status_sequence(
        [
            "MINIMIZER_PROPOSED",
            "DEFENDER_REVIEWED",
            "ADVERSARY_CRITIQUED",
            "ADVERSARY_CHALLENGED",
            "ADVERSARY_FAILED",
            "MINIMIZER_PROPOSED",
            "DEFENDER_REVIEWED",
            "ADVERSARY_CRITIQUED",
            "ADVERSARY_CHALLENGED",
            "ADVERSARY_CHALLENGED",
            "JUDGE_SCORED",
            "DEBATE_CONVERGED",
        ]
    )
    assert errors == []


def test_validate_status_sequence_rejects_impossible_jump():
    errors = validate_status_sequence(["MINIMIZER_PROPOSED", "JUDGE_SCORED"])
    assert errors == ["illegal transition: MINIMIZER_PROPOSED -> JUDGE_SCORED"]


def test_validate_status_sequence_accepts_terminal_failures():
    assert validate_status_sequence(["FAILED"]) == []
    assert validate_status_sequence(["MINIMIZER_PROPOSED", "AST_REJECTED", "FAILED"]) == []
    assert validate_status_sequence(["MINIMIZER_PROPOSED", "DEFENDER_REVIEWED", "PYTEST_FAILED", "FAILED"]) == []
    assert validate_status_sequence(
        [
            "MINIMIZER_PROPOSED",
            "DEFENDER_REVIEWED",
            "ADVERSARY_CRITIQUED",
            "ADVERSARY_CHALLENGED",
            "ADVERSARY_CHALLENGED",
            "JUDGE_SCORED",
            "FAILED",
        ]
    ) == []


def test_should_converge_on_threshold_or_max_rounds():
    assert should_converge(round_number=1, code_change_percent=3.0, max_rounds=3) is True
    assert should_converge(round_number=2, code_change_percent=30.0, max_rounds=3) is False
    assert should_converge(round_number=3, code_change_percent=30.0, max_rounds=3) is True
