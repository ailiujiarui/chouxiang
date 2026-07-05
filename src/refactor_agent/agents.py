from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from refactor_agent.llm import RefactorClient
from refactor_agent.models import LLMRefactorResult, MetricsSnapshot, MutationTestResult, RefactorRequest, RewardBreakdown
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
    """Rule-based adversary that attacks candidates with AST mutation testing."""

    max_mutants: int = 8

    def challenge(
        self,
        candidate_source: str,
        target_file: Path,
        workspace: Path,
        tests_path: Path,
        timeout_seconds: float,
    ) -> MutationTestResult:
        return run_mutation_tests(
            candidate_source=candidate_source,
            target_file=target_file,
            workspace=workspace,
            tests_path=tests_path,
            timeout_seconds=timeout_seconds,
            max_mutants=self.max_mutants,
        )


class JudgeAgent:
    """Scores the final candidate with a multi-objective reward function."""

    def score(
        self,
        pre: MetricsSnapshot,
        post: MetricsSnapshot,
        retry_count: int,
        mutation_result: MutationTestResult | None,
    ) -> RewardBreakdown:
        return calculate_reward(
            pre=pre,
            post=post,
            retry_count=retry_count,
            mutation_result=mutation_result,
        )
