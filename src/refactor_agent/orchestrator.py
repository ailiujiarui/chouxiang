from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.agents import AdversaryAgent, JudgeAgent, MinimizerAgent
from refactor_agent.ast_analyzer import validate_candidate_source
from refactor_agent.llm import LLMError, RefactorClient
from refactor_agent.metrics import analyze_file
from refactor_agent.models import (
    AdversarialTestResult,
    CandidateValidationResult,
    MutationTestResult,
    PerformanceProfile,
    RefactorRequest,
    RefactorRunResult,
    RunRecord,
    SandboxResult,
    TrajectoryStep,
)
from refactor_agent.sandbox import (
    prepare_workspace,
    run_performance_profile_with_backend,
    run_pytest_with_backend,
    write_candidate,
)
from refactor_agent.store import SQLiteRunStore
from refactor_agent.trajectory import append_trajectory


class RefactorOrchestrator:
    def __init__(
        self,
        llm_client: RefactorClient,
        run_root: Path = Path(".runs"),
        store: SQLiteRunStore | None = None,
        pytest_timeout_seconds: float = 30.0,
        sandbox_backend: str = "subprocess",
        sandbox_docker_image: str = "refactor-agent-sandbox:py312",
        sandbox_memory: str = "256m",
        sandbox_cpus: float = 1.0,
    ) -> None:
        self.llm_client = llm_client
        self.run_root = run_root.resolve()
        self.store = store or SQLiteRunStore(self.run_root / "refactor_agent.sqlite")
        self.pytest_timeout_seconds = pytest_timeout_seconds
        self.sandbox_backend = sandbox_backend
        self.sandbox_docker_image = sandbox_docker_image
        self.sandbox_memory = sandbox_memory
        self.sandbox_cpus = sandbox_cpus
        self.minimizer = MinimizerAgent(llm_client)
        self.adversary = AdversaryAgent()
        self.judge = JudgeAgent()

    def run(self, request: RefactorRequest) -> RefactorRunResult:
        run_id = _new_run_id()
        workspace = self.run_root / run_id / "workspace"
        repo_name = request.repo_name or request.target_file.resolve().parent.name
        baseline = analyze_file(request.target_file)
        original_code = request.target_file.read_text(encoding="utf-8")
        _, target_in_workspace, tests_in_workspace = prepare_workspace(
            request.target_file,
            request.tests_path,
            workspace,
        )

        current_code = original_code
        previous_error: str | None = None
        last_sandbox: SandboxResult | None = None
        last_validation: CandidateValidationResult | None = None
        last_adversarial: AdversarialTestResult | None = None
        last_mutation: MutationTestResult | None = None
        last_performance: PerformanceProfile | None = None
        trajectory_path = self.run_root / run_id / "trajectory.jsonl"

        for attempt in range(1, request.max_retry + 1):
            try:
                llm_result = self.minimizer.propose(
                    request=request,
                    current_code=current_code,
                    baseline_metrics=baseline,
                    previous_error=previous_error,
                    attempt=attempt,
                )
            except LLMError as exc:
                record = RunRecord(
                    run_id=run_id,
                    issue_id=request.issue_id,
                    repo_name=repo_name,
                    pre_loc=baseline.loc,
                    pre_cc=baseline.cyclomatic_complexity,
                    self_heal_count=attempt - 1,
                    status="FAILED",
                    error=str(exc),
                )
                self.store.save(record)
                return RefactorRunResult(
                    record=record,
                    report_markdown=_build_report(
                        record,
                        workspace,
                        None,
                        None,
                        str(exc),
                        last_validation,
                        last_adversarial,
                        last_mutation,
                        None,
                        last_performance,
                    ),
                    workspace_path=workspace,
                    attempts=attempt,
                    last_sandbox_result=last_sandbox,
                    candidate_file=target_in_workspace,
                    ast_validation=last_validation,
                    adversarial_result=last_adversarial,
                    mutation_result=last_mutation,
                    performance_profile=last_performance,
                )

            current_code = llm_result.fixed_code
            last_validation = validate_candidate_source(original_code, current_code)
            if not last_validation.ok:
                previous_error = "AST guard rejected candidate before sandbox:\n" + last_validation.summary()
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(attempt=attempt, status="AST_REJECTED", message=previous_error),
                )
                continue

            write_candidate(target_in_workspace, current_code)
            last_sandbox = run_pytest_with_backend(
                workspace=workspace,
                tests_path=tests_in_workspace,
                timeout_seconds=self.pytest_timeout_seconds,
                backend=self.sandbox_backend,
                docker_image=self.sandbox_docker_image,
                memory=self.sandbox_memory,
                cpus=self.sandbox_cpus,
            )
            if last_sandbox.passed:
                last_adversarial = self.adversary.generate_tests(
                    candidate_source=current_code,
                    workspace=workspace,
                    target_file=target_in_workspace,
                    timeout_seconds=self.pytest_timeout_seconds,
                    backend=self.sandbox_backend,
                    docker_image=self.sandbox_docker_image,
                    memory=self.sandbox_memory,
                    cpus=self.sandbox_cpus,
                )
                if not last_adversarial.passed:
                    previous_error = _summarize_adversarial_failure(last_adversarial)
                    append_trajectory(
                        trajectory_path,
                        TrajectoryStep(attempt=attempt, status="ADVERSARY_FAILED", message=previous_error),
                    )
                    continue

                post = analyze_file(target_in_workspace)
                last_mutation = self.adversary.challenge(
                    candidate_source=current_code,
                    target_file=target_in_workspace,
                    workspace=workspace,
                    tests_path=tests_in_workspace,
                    timeout_seconds=self.pytest_timeout_seconds,
                    backend=self.sandbox_backend,
                    docker_image=self.sandbox_docker_image,
                    memory=self.sandbox_memory,
                    cpus=self.sandbox_cpus,
                )
                last_performance = run_performance_profile_with_backend(
                    workspace=workspace,
                    target_file=target_in_workspace,
                    tests_path=tests_in_workspace,
                    timeout_seconds=self.pytest_timeout_seconds,
                    backend=self.sandbox_backend,
                    docker_image=self.sandbox_docker_image,
                    memory=self.sandbox_memory,
                    cpus=self.sandbox_cpus,
                )
                reward = self.judge.score(
                    pre=baseline,
                    post=post,
                    retry_count=attempt - 1,
                    mutation_result=last_mutation,
                    adversarial_result=last_adversarial,
                )
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="SUCCESS",
                        message="pytest, mutation testing, and performance profiling completed",
                        reward=reward,
                    ),
                )
                record = RunRecord(
                    run_id=run_id,
                    issue_id=request.issue_id,
                    repo_name=repo_name,
                    pre_loc=baseline.loc,
                    post_loc=post.loc,
                    pre_cc=baseline.cyclomatic_complexity,
                    post_cc=post.cyclomatic_complexity,
                    self_heal_count=attempt - 1,
                    status="SUCCESS",
                )
                self.store.save(record)
                return RefactorRunResult(
                    record=record,
                    report_markdown=_build_report(
                        record,
                        workspace,
                        llm_result.insult_review,
                        last_sandbox,
                        None,
                        last_validation,
                        last_adversarial,
                        last_mutation,
                        reward,
                        last_performance,
                    ),
                    workspace_path=workspace,
                    attempts=attempt,
                    last_sandbox_result=last_sandbox,
                    candidate_file=target_in_workspace,
                    ast_validation=last_validation,
                    adversarial_result=last_adversarial,
                    mutation_result=last_mutation,
                    performance_profile=last_performance,
                )

            previous_error = _summarize_failure(last_sandbox)
            append_trajectory(
                trajectory_path,
                TrajectoryStep(attempt=attempt, status="PYTEST_FAILED", message=previous_error),
            )

        record = RunRecord(
            run_id=run_id,
            issue_id=request.issue_id,
            repo_name=repo_name,
            pre_loc=baseline.loc,
            pre_cc=baseline.cyclomatic_complexity,
            self_heal_count=request.max_retry,
            status="FAILED",
            error=previous_error or "pytest failed",
        )
        self.store.save(record)
        append_trajectory(
            trajectory_path,
            TrajectoryStep(attempt=request.max_retry, status="FAILED", message=record.error or "refactor failed"),
        )
        return RefactorRunResult(
            record=record,
            report_markdown=_build_report(
                record,
                workspace,
                None,
                last_sandbox,
                record.error,
                last_validation,
                last_adversarial,
                last_mutation,
                None,
                last_performance,
            ),
            workspace_path=workspace,
            attempts=request.max_retry,
            last_sandbox_result=last_sandbox,
            candidate_file=target_in_workspace,
            ast_validation=last_validation,
            adversarial_result=last_adversarial,
            mutation_result=last_mutation,
            performance_profile=last_performance,
        )


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"


def _summarize_failure(result: SandboxResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return combined[-8000:] if combined else f"pytest failed with return code {result.returncode}"


def _summarize_adversarial_failure(result: AdversarialTestResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return (
        "Adversary generated tests failed:\n"
        + (combined[-8000:] if combined else f"pytest failed with return code {result.returncode}")
    )


def _build_report(
    record: RunRecord,
    workspace: Path,
    review: str | None,
    sandbox_result: SandboxResult | None,
    error: str | None,
    ast_validation: CandidateValidationResult | None = None,
    adversarial_result: AdversarialTestResult | None = None,
    mutation_result: MutationTestResult | None = None,
    reward=None,
    performance_profile: PerformanceProfile | None = None,
) -> str:
    loc_delta = _delta(record.pre_loc, record.post_loc)
    cc_delta = _delta(record.pre_cc, record.post_cc)
    lines = [
        "### Refactor Agent Report",
        "",
        f"- Status: **{record.status}**",
        f"- Run ID: `{record.run_id}`",
        f"- Workspace: `{workspace}`",
        f"- Self-heal count: {record.self_heal_count}",
        f"- LOC: {record.pre_loc} -> {record.post_loc} ({loc_delta})",
        f"- Cyclomatic Complexity: {record.pre_cc} -> {record.post_cc} ({cc_delta})",
    ]
    if sandbox_result is not None:
        lines.extend(
            [
                f"- Pytest return code: {sandbox_result.returncode}",
                f"- Pytest duration: {sandbox_result.duration_seconds:.2f}s",
            ]
        )
    if ast_validation is not None:
        lines.extend(["- AST guard: passed" if ast_validation.ok else "- AST guard: rejected"])
    if adversarial_result is not None:
        lines.append(
            "- Adversarial tests: "
            f"{adversarial_result.generated} generated, "
            f"{'passed' if adversarial_result.passed else 'failed'}"
        )
    if mutation_result is not None:
        lines.append(
            "- Mutation testing: "
            f"{mutation_result.killed}/{mutation_result.total} killed "
            f"({mutation_result.kill_rate * 100:.1f}% kill rate)"
        )
    if performance_profile is not None:
        import_time = (
            f"{performance_profile.import_time_seconds:.4f}s"
            if performance_profile.import_time_seconds is not None
            else "n/a"
        )
        lines.extend(
            [
                f"- Profiled pytest duration: {performance_profile.pytest_duration_seconds:.2f}s",
                f"- Peak traced memory: {performance_profile.peak_memory_kib:.1f} KiB",
                f"- Module import time: {import_time}",
            ]
        )
    if reward is not None:
        lines.append(f"- Reward: {reward.reward:.2f}")
    if mutation_result and mutation_result.survival_details:
        lines.extend(["", "#### Surviving Mutants", ""])
        lines.extend(f"- {detail}" for detail in mutation_result.survival_details)
    if review:
        lines.extend(["", "#### Code Review", "", review])
    if error:
        lines.extend(["", "#### Error", "", "```text", error[-4000:], "```"])
    return "\n".join(lines)


def _delta(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    change = after - before
    if before == 0:
        return f"{change:+d}"
    percentage = (change / before) * 100
    return f"{change:+d}, {percentage:+.1f}%"
