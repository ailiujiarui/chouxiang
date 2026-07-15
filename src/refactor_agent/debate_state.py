from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from refactor_agent.execution_graph import render_execution_mermaid


@dataclass(frozen=True)
class DebateTransition:
    source: str
    target: str
    label: str


DEBATE_TRANSITIONS = [
    DebateTransition("START", "FAILED", "terminal setup or LLM failure"),
    DebateTransition("START", "MINIMIZER_PROPOSED", "生成候选"),
    DebateTransition("MINIMIZER_PROPOSED", "DEFENDER_REVIEWED", "AST 通过"),
    DebateTransition("MINIMIZER_PROPOSED", "AST_REJECTED", "AST 失败"),
    DebateTransition("DEFENDER_REVIEWED", "AST_REJECTED", "AST 失败"),
    DebateTransition("DEFENDER_REVIEWED", "PYTEST_FAILED", "pytest 失败"),
    DebateTransition("DEFENDER_REVIEWED", "ADVERSARY_CRITIQUED", "pytest 通过"),
    DebateTransition("ADVERSARY_CRITIQUED", "ADVERSARY_CHALLENGED", "生成反例测试"),
    DebateTransition("ADVERSARY_CHALLENGED", "ADVERSARY_CHALLENGED", "变异攻击"),
    DebateTransition("ADVERSARY_CHALLENGED", "ADVERSARY_FAILED", "发现反例"),
    DebateTransition("ADVERSARY_CHALLENGED", "JUDGE_SCORED", "扛过攻击"),
    DebateTransition("JUDGE_SCORED", "DEBATE_CONVERGED", "接受奖励分"),
    DebateTransition("JUDGE_SCORED", "FAILED", "terminal rejection"),
    DebateTransition("AST_REJECTED", "MINIMIZER_PROPOSED", "回炉重试"),
    DebateTransition("PYTEST_FAILED", "MINIMIZER_PROPOSED", "回炉重试"),
    DebateTransition("ADVERSARY_FAILED", "MINIMIZER_PROPOSED", "带着红队意见重试"),
    DebateTransition("AST_REJECTED", "FAILED", "retry limit reached"),
    DebateTransition("PYTEST_FAILED", "FAILED", "retry limit reached"),
    DebateTransition("ADVERSARY_FAILED", "FAILED", "retry limit reached"),
    DebateTransition("DEBATE_CONVERGED", "SUCCESS", "收尾"),
]

TERMINAL_STATES = {"SUCCESS", "FAILED"}
RETRY_STATES = {"AST_REJECTED", "PYTEST_FAILED", "ADVERSARY_FAILED"}


def render_mermaid_state_diagram() -> str:
    return render_execution_mermaid()


def legal_transition_pairs() -> set[tuple[str, str]]:
    return {(transition.source, transition.target) for transition in DEBATE_TRANSITIONS}


def validate_status_sequence(statuses: Iterable[str]) -> list[str]:
    sequence = [status for status in statuses if status]
    if not sequence:
        return ["trajectory is empty"]

    errors: list[str] = []
    allowed = legal_transition_pairs()
    previous = "START"
    for status in sequence:
        if (previous, status) in allowed:
            previous = status
            continue
        if previous in RETRY_STATES and status == "MINIMIZER_PROPOSED":
            previous = status
            continue
        if previous == "DEBATE_CONVERGED" and status in TERMINAL_STATES:
            previous = status
            continue
        errors.append(f"illegal transition: {previous} -> {status}")
        previous = status
    return errors


def should_converge(
    round_number: int,
    code_change_percent: float | None,
    max_rounds: int,
    change_threshold_percent: float = 5.0,
) -> bool:
    if round_number >= max_rounds:
        return True
    if code_change_percent is None:
        return False
    return code_change_percent < change_threshold_percent
