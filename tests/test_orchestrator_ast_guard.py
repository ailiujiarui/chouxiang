import pytest

from refactor_agent.analysis_events import AnalysisEventType
from refactor_agent.models import (
    AgentDebateMessage,
    AstRewriteResult,
    CandidateValidationResult,
    LLMRefactorResult,
    SafetyFinding,
    TargetRegion,
)
from refactor_agent.orchestrator_ast_guard import (
    code_change_percent,
    guard_ast_execution_node,
    rewrite_metadata,
)
from refactor_agent.orchestrator_state import initial_execution_state


class Defender:
    def review_static(self, validation: CandidateValidationResult) -> str:
        return "accepted" if validation.ok else "rejected: " + validation.summary()


def test_ast_guard_accepts_candidate_and_records_rewrite_metadata(monkeypatch):
    state = _state(max_attempts=2)
    rewrite = _rewrite(ok=True)
    validation = CandidateValidationResult(ok=True)
    monkeypatch.setattr(
        "refactor_agent.orchestrator_ast_guard.controlled_subtree_rewrite",
        lambda *args: rewrite,
    )
    monkeypatch.setattr(
        "refactor_agent.orchestrator_ast_guard.validate_candidate_source",
        lambda *args: validation,
    )
    events: list[tuple] = []
    trajectory: list[tuple] = []

    returned = guard_ast_execution_node(
        state,
        allowed_import_roots={"math"},
        defender=Defender(),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert returned is state
    assert state["rewrite"] == rewrite
    assert state["validation"] == validation
    assert state["current_code"] == rewrite.source
    assert state["previous_candidate_code"] == rewrite.source
    assert state["code_change_percent"] == pytest.approx(
        code_change_percent(state["original_code"], rewrite.source)
    )
    assert state["next_node"] == "pytest"
    assert state["round_messages"][-1] == AgentDebateMessage(
        round=1,
        agent="DEFENDER",
        content="accepted",
    )
    assert events == []
    assert trajectory == [
        (
            state,
            "DEFENDER_REVIEWED",
            "accepted",
            "DEFENDER",
            rewrite_metadata(rewrite),
        )
    ]


@pytest.mark.parametrize(
    ("attempt", "expected_next", "recoverable"),
    [(1, "minimizer", True), (2, "finalize", False)],
)
def test_ast_guard_rejection_closes_round_and_routes_retry_or_finalize(
    monkeypatch,
    attempt,
    expected_next,
    recoverable,
):
    state = _state(max_attempts=2)
    state["attempt"] = attempt
    finding = SafetyFinding(rule="public-api", message="signature changed")
    rewrite = _rewrite(ok=False, findings=[finding])
    monkeypatch.setattr(
        "refactor_agent.orchestrator_ast_guard.controlled_subtree_rewrite",
        lambda *args: rewrite,
    )
    monkeypatch.setattr(
        "refactor_agent.orchestrator_ast_guard.validate_candidate_source",
        lambda *args: CandidateValidationResult(ok=True),
    )
    events: list[tuple] = []
    trajectory: list[tuple] = []

    guard_ast_execution_node(
        state,
        allowed_import_roots=set(),
        defender=Defender(),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: trajectory.append(args),
    )

    assert state["validation"].ok is False
    assert "public-api" in state["previous_error"]
    assert state["next_node"] == expected_next
    assert len(state["debate_rounds"]) == 1
    assert state["debate_rounds"][0].round == attempt
    assert events == [
        (
            (AnalysisEventType.AST_REJECTED, state),
            {
                "phase": "ast_guard",
                "error_category": "ast_guard_rejected",
                "recoverable": recoverable,
            },
        )
    ]
    assert trajectory[0][1:] == (
        "AST_REJECTED",
        state["previous_error"],
        "DEFENDER",
        rewrite_metadata(rewrite),
    )


def test_code_change_percent_positive():
    expected = code_change_percent("value = 1\n", "value = 2\n")

    assert expected > 0


def _real_state(original_code: str, fixed_code: str, max_attempts: int = 2):
    state = initial_execution_state(max_attempts)
    state.update(
        {
            "attempt": 1,
            "original_code": original_code,
            "current_code": original_code,
            "allowed_regions": ["value"],
            "llm_result": LLMRefactorResult(
                thought="candidate",
                fixed_code=fixed_code,
                insult_review="review",
            ),
            "round_messages": [
                AgentDebateMessage(round=1, agent="MINIMIZER", content="candidate")
            ],
        }
    )
    return state


def _run_real_guard(state, allowed_import_roots: set[str] = set()):
    events: list[tuple] = []
    trajectory: list[tuple] = []

    guard_ast_execution_node(
        state,
        allowed_import_roots=allowed_import_roots,
        defender=Defender(),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
        record_trajectory=lambda *args: trajectory.append(args),
    )
    return events, trajectory


def test_real_guard_rejects_dangerous_import_and_call_in_candidate():
    """Prompt-injection-style candidate must be stopped by the structural guard."""
    original = "def value(flag):\n    return 1 if flag else 0\n"
    candidate = "def value(flag):\n    import os\n    os.system('id')\n    return int(flag)\n"
    state = _real_state(original, candidate)

    events, trajectory = _run_real_guard(state)

    assert state["rewrite"].ok is False
    rules = {item.rule for item in state["rewrite"].findings}
    assert "import-not-allowlisted" in rules or "blocked-call" in rules
    assert state["validation"].ok is False
    assert state["previous_error"].startswith("AST guard rejected candidate:")
    assert state["next_node"] in {"minimizer", "finalize"}
    assert events[0][0][0] is AnalysisEventType.AST_REJECTED
    assert trajectory[0][1] == "AST_REJECTED"


def test_real_guard_rejects_change_outside_allowed_region():
    original = (
        "def value(flag):\n"
        "    return 1 if flag else 0\n"
        "\n"
        "def helper(x):\n"
        "    return x * 2\n"
    )
    candidate = (
        "def value(flag):\n"
        "    return int(flag)\n"
        "\n"
        "def helper(x):\n"
        "    return x * 999\n"
    )
    state = _real_state(original, candidate)

    _run_real_guard(state)

    assert state["rewrite"].ok is False
    rules = {item.rule for item in state["rewrite"].findings}
    assert "non-target-changed" in rules
    assert state["validation"].ok is False
    assert state["next_node"] in {"minimizer", "finalize"}


def test_real_guard_rejects_new_public_symbol():
    original = "def value(flag):\n    return 1 if flag else 0\n"
    candidate = (
        "def value(flag):\n"
        "    return int(flag)\n"
        "\n"
        "def backdoor():\n"
        "    return 0\n"
    )
    state = _real_state(original, candidate)

    _run_real_guard(state)

    assert state["rewrite"].ok is False
    rules = {item.rule for item in state["rewrite"].findings}
    assert "public-api-added" in rules or "non-target-changed" in rules
    assert state["validation"].ok is False
    assert state["next_node"] in {"minimizer", "finalize"}


def test_real_guard_rejects_signature_change():
    original = "def value(flag):\n    return 1 if flag else 0\n"
    candidate = "def value(flag, extra=False):\n    return int(flag)\n"
    state = _real_state(original, candidate)

    _run_real_guard(state)

    assert state["rewrite"].ok is False
    rules = {item.rule for item in state["rewrite"].findings}
    assert "signature-changed" in rules
    assert state["next_node"] in {"minimizer", "finalize"}


def test_real_guard_accepts_legitimate_simplification():
    original = (
        "def value(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    else:\n"
        "        return 0\n"
    )
    candidate = "def value(flag):\n    return 1 if flag else 0\n"
    state = _real_state(original, candidate)

    events, trajectory = _run_real_guard(state)

    assert state["rewrite"].ok is True
    assert state["validation"].ok is True
    assert state["current_code"] == candidate
    assert state["next_node"] == "pytest"
    assert events == []
    assert trajectory[0][1] == "DEFENDER_REVIEWED"


def _state(max_attempts: int):
    state = initial_execution_state(max_attempts)
    state.update(
        {
            "attempt": 1,
            "original_code": "def value(flag):\n    return 1 if flag else 0\n",
            "current_code": "def value(flag):\n    return 1 if flag else 0\n",
            "allowed_regions": ["value"],
            "llm_result": LLMRefactorResult(
                thought="candidate",
                fixed_code="def value(flag):\n    return int(flag)\n",
                insult_review="review",
            ),
            "round_messages": [
                AgentDebateMessage(round=1, agent="MINIMIZER", content="candidate")
            ],
        }
    )
    return state


def _rewrite(
    *,
    ok: bool,
    findings: list[SafetyFinding] | None = None,
) -> AstRewriteResult:
    return AstRewriteResult(
        ok=ok,
        source="def value(flag):\n    return int(flag)\n",
        selected_regions=[
            TargetRegion(
                qualified_name="value",
                lineno=1,
                end_lineno=2,
                complexity=2,
                node_count=8,
                structural_entropy=1.0,
            )
        ],
        allowed_regions=["value"],
        changed_regions=["value"],
        added_imports=[],
        findings=findings or [],
    )
