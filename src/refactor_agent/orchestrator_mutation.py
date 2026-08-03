from __future__ import annotations

from pathlib import Path
import shutil
from typing import Protocol

from refactor_agent.execution_control import ExecutionControl
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.metrics import analyze_file
from refactor_agent.models import AgentDebateMessage, MutationTestResult
from refactor_agent.orchestrator_state import transition_to
from refactor_agent.sandbox import run_performance_profile_with_backend


class MutationAdversary(Protocol):
    def challenge(
        self,
        candidate_source: str,
        target_file: Path,
        workspace: Path,
        tests_path: Path,
        timeout_seconds: float,
        backend: str,
        docker_image: str,
        memory: str,
        cpus: float,
        execution_control: ExecutionControl,
        target_regions: list[str],
    ) -> MutationTestResult: ...


class TrajectoryRecorder(Protocol):
    def __call__(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
    ) -> None: ...


def run_mutation_execution_node(
    state: ExecutionState,
    *,
    workspace: Path,
    timeout_seconds: float,
    docker_image: str,
    docker_memory: str,
    docker_cpus: float,
    execution_control: ExecutionControl,
    adversary: MutationAdversary,
    record_trajectory: TrajectoryRecorder,
) -> ExecutionState:
    """Run mutation and performance evidence before handing the state to Judge."""
    state["post"] = analyze_file(state["target_file"])
    mutation_tests = combined_mutation_tests_path(
        workspace,
        state["tests_path"],
        state["adversarial"].test_file,
    )
    state["mutation"] = adversary.challenge(
        candidate_source=state["current_code"],
        target_file=state["target_file"],
        workspace=workspace,
        tests_path=mutation_tests,
        timeout_seconds=timeout_seconds,
        backend=state["active_backend"],
        docker_image=docker_image,
        memory=docker_memory,
        cpus=docker_cpus,
        execution_control=execution_control,
        target_regions=state["rewrite"].changed_regions,
    )
    message = summarize_mutation(state["mutation"])
    state["round_messages"].append(
        AgentDebateMessage(
            round=state["attempt"],
            agent="ADVERSARY",
            content=message,
        )
    )
    record_trajectory(state, "ADVERSARY_CHALLENGED", message, "ADVERSARY")
    state["performance"] = run_performance_profile_with_backend(
        workspace=workspace,
        target_file=state["target_file"],
        tests_path=state["tests_path"],
        timeout_seconds=timeout_seconds,
        backend=state["active_backend"],
        docker_image=docker_image,
        memory=docker_memory,
        cpus=docker_cpus,
        execution_control=execution_control,
    )
    return transition_to(state, "judge")


def summarize_mutation(result: MutationTestResult) -> str:
    return (
        f"变异攻击击杀 {result.killed}/{result.total} 个变异体"
        f"（击杀率 {result.kill_rate * 100:.1f}%）。"
    )


def combined_mutation_tests_path(
    workspace: Path,
    baseline_tests: Path,
    adversarial_test_file: Path | None,
) -> Path:
    if adversarial_test_file is None or not adversarial_test_file.is_file():
        return baseline_tests
    combined = workspace / "_mutation_tests"
    if combined.exists():
        shutil.rmtree(combined)
    baseline_target = combined / "baseline"
    adversarial_target = combined / "adversarial"
    baseline_target.mkdir(parents=True)
    adversarial_target.mkdir(parents=True)
    if baseline_tests.is_dir():
        shutil.copytree(baseline_tests, baseline_target, dirs_exist_ok=True)
    else:
        shutil.copy2(baseline_tests, baseline_target / baseline_tests.name)
    shutil.copy2(
        adversarial_test_file,
        adversarial_target / adversarial_test_file.name,
    )
    return combined
