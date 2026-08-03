from __future__ import annotations

from pathlib import Path
from typing import Protocol

from refactor_agent.analysis_events import AnalysisEventType, SafeMetric
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.models import (
    AdversarialCritique,
    AdversarialTestResult,
    AgentDebateMessage,
)
from refactor_agent.orchestrator_state import (
    close_debate_round,
    retry_or_finalize,
    transition_to,
)


class Adversary(Protocol):
    def critique(self, candidate_source: str, issue_text: str) -> AdversarialCritique: ...

    def generate_tests(
        self,
        candidate_source: str,
        workspace: Path,
        target_file: Path,
        issue_text: str,
        timeout_seconds: float,
        backend: str,
        docker_image: str,
        memory: str,
        cpus: float,
        execution_control: ExecutionControl,
    ) -> AdversarialTestResult: ...


class AnalysisEventEmitter(Protocol):
    def __call__(
        self,
        event_type: AnalysisEventType,
        state: ExecutionState,
        *,
        phase: str | None = None,
        error_category: str | None = None,
        recoverable: bool | None = None,
        safe_metrics: dict[str, SafeMetric] | None = None,
    ) -> None: ...


class TrajectoryRecorder(Protocol):
    def __call__(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
    ) -> None: ...


def run_adversary_execution_node(
    state: ExecutionState,
    *,
    issue_text: str,
    workspace: Path,
    timeout_seconds: float,
    docker_image: str,
    docker_memory: str,
    docker_cpus: float,
    execution_control: ExecutionControl,
    adversary: Adversary,
    emit_analysis_event: AnalysisEventEmitter,
    record_trajectory: TrajectoryRecorder,
) -> ExecutionState:
    """Critique a candidate, generate boundary tests, and route their verdict."""
    critique = adversary.critique(state["current_code"], issue_text)
    critique_message = summarize_critique(critique)
    state["round_messages"].append(
        AgentDebateMessage(
            round=state["attempt"],
            agent="ADVERSARY",
            content=critique_message,
        )
    )
    record_trajectory(
        state,
        "ADVERSARY_CRITIQUED",
        critique_message,
        "ADVERSARY",
    )

    result = adversary.generate_tests(
        candidate_source=state["current_code"],
        workspace=workspace,
        target_file=state["target_file"],
        issue_text=issue_text,
        timeout_seconds=timeout_seconds,
        backend=state["active_backend"],
        docker_image=docker_image,
        memory=docker_memory,
        cpus=docker_cpus,
        execution_control=execution_control,
    )
    state["adversarial"] = result
    message = summarize_adversary_pass(result)
    state["round_messages"].append(
        AgentDebateMessage(
            round=state["attempt"],
            agent="ADVERSARY",
            content=message,
        )
    )
    record_trajectory(
        state,
        "ADVERSARY_CHALLENGED",
        message,
        "ADVERSARY",
    )

    if not result.passed:
        state["previous_error"] = (
            summarize_adversarial_failure(result) + "\n" + critique_message
        )
        emit_analysis_event(
            AnalysisEventType.ADVERSARY_FAILED,
            state,
            phase="adversary",
            error_category="adversary_failed",
            recoverable=state["attempt"] < state["max_attempts"],
            safe_metrics={"generated_tests": result.generated},
        )
        record_trajectory(
            state,
            "ADVERSARY_FAILED",
            state["previous_error"],
            "ADVERSARY",
        )
        close_debate_round(
            state,
            pytest_passed=True,
            adversarial_passed=False,
        )
        return retry_or_finalize(state)

    emit_analysis_event(
        AnalysisEventType.ADVERSARY_PASSED,
        state,
        phase="adversary",
        safe_metrics={"generated_tests": result.generated},
    )
    return transition_to(state, "mutation")


def summarize_adversarial_failure(result: AdversarialTestResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return (
        "对抗 Agent 生成的测试失败：\n"
        + (combined[-8000:] if combined else f"pytest 失败，返回码 {result.returncode}")
    )


def summarize_adversary_pass(result: AdversarialTestResult) -> str:
    if result.generated == 0:
        return "对抗 Agent 没找到可自动生成的规则边界测试，只能先把变异测试请上桌。"
    status = "通过" if result.passed else "失败"
    return f"对抗 Agent 生成 {result.generated} 个边界测试；候选代码结果：{status}。"


def summarize_critique(critique: AdversarialCritique) -> str:
    plan = "; ".join(critique.attack_plan)
    hint = f" 反例提示：{critique.counterexample_hint}" if critique.counterexample_hint else ""
    return f"红队风险={critique.risk_level}。攻击计划：{plan or '暂无命中规则'}{hint}"
