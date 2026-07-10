from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from refactor_agent.adversary import critique_candidate, run_adversarial_tests
from refactor_agent.llm import RefactorClient
from refactor_agent.models import (
    AdversarialCritique,
    AdversarialTestResult,
    CandidateValidationResult,
    LLMRefactorResult,
    MetricsSnapshot,
    MutationTestResult,
    RefactorRequest,
    RewardBreakdown,
    SandboxResult,
)
from refactor_agent.mutation import run_mutation_tests
from refactor_agent.trajectory import calculate_reward


@dataclass
class MinimizerAgent:
    """Aggressive code minimizer backed by the configured LLM client."""

    client: RefactorClient

    def propose(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        return self.client.refactor(
            request=request,
            current_code=current_code,
            baseline_metrics=baseline_metrics,
            previous_error=previous_error,
            attempt=attempt,
        )


@dataclass
class AdversaryAgent:
    """Rule-based adversary that attacks candidates with generated tests and mutation testing."""

    max_mutants: int = 8

    def critique(self, candidate_source: str, issue_text: str) -> AdversarialCritique:
        return critique_candidate(candidate_source, issue_text)

    def challenge(
        self,
        candidate_source: str,
        target_file: Path,
        workspace: Path,
        tests_path: Path,
        timeout_seconds: float,
        backend: str = "subprocess",
        docker_image: str = "refactor-agent-sandbox:py312",
        memory: str = "256m",
        cpus: float = 1.0,
    ) -> MutationTestResult:
        return run_mutation_tests(
            candidate_source=candidate_source,
            target_file=target_file,
            workspace=workspace,
            tests_path=tests_path,
            timeout_seconds=timeout_seconds,
            max_mutants=self.max_mutants,
            backend=backend,
            docker_image=docker_image,
            memory=memory,
            cpus=cpus,
        )

    def generate_tests(
        self,
        candidate_source: str,
        workspace: Path,
        target_file: Path,
        timeout_seconds: float,
        issue_text: str = "",
        backend: str = "subprocess",
        docker_image: str = "refactor-agent-sandbox:py312",
        memory: str = "256m",
        cpus: float = 1.0,
    ) -> AdversarialTestResult:
        return run_adversarial_tests(
            candidate_source=candidate_source,
            workspace=workspace,
            target_file=target_file,
            issue_text=issue_text,
            timeout_seconds=timeout_seconds,
            backend=backend,
            docker_image=docker_image,
            memory=memory,
            cpus=cpus,
        )


class DefenderAgent:
    """Conservative reviewer that protects syntax, public API, and regression tests."""

    def review_static(self, validation: CandidateValidationResult) -> str:
        if validation.ok:
            return "AST 守卫通过：语法、安全检查和公开 API 都没被候选代码拆坏。"
        return "AST 守卫拒绝候选代码：\n" + validation.summary()

    def review_pytest(self, result: SandboxResult) -> str:
        if result.passed:
            return f"回归测试通过，用时 {result.duration_seconds:.2f}s。"
        return f"回归测试失败，返回码 {result.returncode}。"


class JudgeAgent:
    """Scores the final candidate with a multi-objective reward function."""

    def score(
        self,
        pre: MetricsSnapshot,
        post: MetricsSnapshot,
        retry_count: int,
        mutation_result: MutationTestResult | None,
        adversarial_result: AdversarialTestResult | None = None,
    ) -> RewardBreakdown:
        return calculate_reward(
            pre=pre,
            post=post,
            retry_count=retry_count,
            mutation_result=mutation_result,
            adversarial_result=adversarial_result,
        )
