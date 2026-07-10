from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
import shutil
from uuid import uuid4

from refactor_agent.agents import AdversaryAgent, DefenderAgent, JudgeAgent, MinimizerAgent
from refactor_agent.ast_analyzer import controlled_subtree_rewrite, select_target_regions, validate_candidate_source
from refactor_agent.debate_graph import DebateGraphState, run_debate_graph
from refactor_agent.debate_state import render_mermaid_state_diagram, should_converge
from refactor_agent.llm import LLMError, RefactorClient
from refactor_agent.memory import build_memory_context, failure_memory, success_memory, target_memory_key
from refactor_agent.metrics import analyze_file
from refactor_agent.models import (
    AdversarialCritique,
    AdversarialTestResult,
    AgentDebateMessage,
    CandidateValidationResult,
    DebateRound,
    MutationTestResult,
    PerformanceProfile,
    RefactorRequest,
    RefactorRunResult,
    RewardBreakdown,
    RunRecord,
    SandboxResult,
    TrajectoryStep,
)
from refactor_agent.sandbox import (
    prepare_workspace,
    resolve_sandbox_backend,
    run_performance_profile_with_backend,
    run_pytest_with_backend,
    SandboxUnavailableError,
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
        graph_backend: str = "langgraph",
    ) -> None:
        self.llm_client = llm_client
        self.run_root = run_root.resolve()
        self.store = store or SQLiteRunStore(self.run_root / "refactor_agent.sqlite")
        self.pytest_timeout_seconds = pytest_timeout_seconds
        self.sandbox_backend = sandbox_backend
        self.sandbox_docker_image = sandbox_docker_image
        self.sandbox_memory = sandbox_memory
        self.sandbox_cpus = sandbox_cpus
        if graph_backend not in {"langgraph", "loop"}:
            raise ValueError(f"Unsupported graph backend: {graph_backend}")
        self.graph_backend = graph_backend
        self.minimizer = MinimizerAgent(llm_client)
        self.defender = DefenderAgent()
        self.adversary = AdversaryAgent()
        self.judge = JudgeAgent()

    def _decide_graph(
        self,
        *,
        attempt: int,
        max_attempts: int,
        ast_ok: bool,
        pytest_passed: bool,
        adversarial_passed: bool | None = None,
        mutation_kill_rate: float | None = None,
        reward: float | None = None,
        failure_feedback: str | None = None,
    ) -> DebateGraphState:
        return run_debate_graph(
            attempt=attempt,
            max_attempts=max_attempts,
            ast_ok=ast_ok,
            pytest_passed=pytest_passed,
            adversarial_passed=adversarial_passed,
            mutation_kill_rate=mutation_kill_rate,
            reward=reward,
            failure_feedback=failure_feedback,
            backend=self.graph_backend,
        )

    def run(self, request: RefactorRequest) -> RefactorRunResult:
        run_id = _new_run_id()
        workspace = self.run_root / run_id / "workspace"
        repo_name = request.repo_name or request.target_file.resolve().parent.name
        memory_key = target_memory_key(request.target_file)
        memory_context = build_memory_context(self.store.list_memory(repo_name, memory_key, limit=3))
        llm_request = _request_with_memory(request, memory_context)
        baseline = analyze_file(request.target_file)
        original_code = request.target_file.read_text(encoding="utf-8")
        allowed_regions = select_target_regions(original_code)
        _, target_in_workspace, tests_in_workspace = prepare_workspace(
            request.target_file,
            request.tests_path,
            workspace,
        )
        trajectory_path = self.run_root / run_id / "trajectory.jsonl"
        try:
            active_backend, _ = resolve_sandbox_backend(self.sandbox_backend)
        except SandboxUnavailableError as exc:
            record = RunRecord(
                run_id=run_id,
                issue_id=request.issue_id,
                repo_name=repo_name,
                pre_loc=baseline.loc,
                pre_cc=baseline.cyclomatic_complexity,
                self_heal_count=0,
                status="FAILED",
                error=str(exc),
            )
            self.store.save(record)
            append_trajectory(
                trajectory_path,
                TrajectoryStep(attempt=0, status="FAILED", message=str(exc)),
            )
            return RefactorRunResult(
                record=record,
                report_markdown=_build_report(
                    record,
                    workspace,
                    None,
                    None,
                    str(exc),
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                workspace_path=workspace,
                attempts=0,
                last_sandbox_result=None,
                candidate_file=target_in_workspace,
            )

        current_code = original_code
        previous_error: str | None = None
        last_sandbox: SandboxResult | None = None
        last_validation: CandidateValidationResult | None = None
        last_adversarial: AdversarialTestResult | None = None
        last_mutation: MutationTestResult | None = None
        last_performance: PerformanceProfile | None = None
        previous_candidate_code: str | None = None
        debate_rounds: list[DebateRound] = []

        for attempt in range(1, request.max_retry + 1):
            try:
                llm_result = self.minimizer.propose(
                    request=llm_request,
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
                self.store.save_memory(failure_memory(record, memory_key))
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
                        debate_rounds,
                    ),
                    workspace_path=workspace,
                    attempts=attempt,
                    last_sandbox_result=last_sandbox,
                    candidate_file=target_in_workspace,
                    ast_validation=last_validation,
                    adversarial_result=last_adversarial,
                    mutation_result=last_mutation,
                    performance_profile=last_performance,
                    debate_rounds=debate_rounds,
                )

            rewrite = controlled_subtree_rewrite(original_code, llm_result.fixed_code, allowed_regions)
            current_code = rewrite.source
            code_change_percent = _code_change_percent(previous_candidate_code or original_code, current_code)
            previous_candidate_code = current_code
            round_messages = [
                AgentDebateMessage(
                    round=attempt,
                    agent="MINIMIZER",
                    content=llm_result.thought,
                    metadata={
                        "review": llm_result.insult_review,
                        "code_change_percent": code_change_percent,
                    },
                )
            ]
            append_trajectory(
                trajectory_path,
                TrajectoryStep(
                    attempt=attempt,
                    status="MINIMIZER_PROPOSED",
                    agent="MINIMIZER",
                    message=llm_result.thought,
                    metadata={
                        "review": llm_result.insult_review,
                        "code_change_percent": code_change_percent,
                    },
                ),
            )

            last_validation = validate_candidate_source(original_code, current_code)
            if not rewrite.ok:
                last_validation = CandidateValidationResult(ok=False, findings=rewrite.findings)
            if not last_validation.ok:
                graph_state = self._decide_graph(
                    attempt=attempt,
                    max_attempts=request.max_retry,
                    ast_ok=False,
                    pytest_passed=False,
                    failure_feedback=last_validation.summary(),
                )
                previous_error = "AST 守卫在进入沙箱前拒绝候选代码：\n" + last_validation.summary()
                defender_message = self.defender.review_static(last_validation)
                round_messages.append(
                    AgentDebateMessage(round=attempt, agent="DEFENDER", content=defender_message)
                )
                debate_rounds.append(
                    DebateRound(
                        round=attempt,
                        code_change_percent=code_change_percent,
                        converged=False,
                        messages=round_messages,
                    )
                )
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="AST_REJECTED",
                        agent="DEFENDER",
                        message=previous_error,
                        metadata={
                            "findings": [finding.model_dump(mode="json") for finding in last_validation.findings],
                            "graph": _graph_metadata(graph_state, self.graph_backend),
                        },
                    ),
                )
                continue
            defender_message = self.defender.review_static(last_validation)
            round_messages.append(
                AgentDebateMessage(round=attempt, agent="DEFENDER", content=defender_message)
            )
            append_trajectory(
                trajectory_path,
                TrajectoryStep(
                    attempt=attempt,
                    status="DEFENDER_REVIEWED",
                    agent="DEFENDER",
                    message=defender_message,
                ),
            )

            write_candidate(target_in_workspace, current_code)
            last_sandbox = run_pytest_with_backend(
                workspace=workspace,
                tests_path=tests_in_workspace,
                timeout_seconds=self.pytest_timeout_seconds,
                backend=active_backend,
                docker_image=self.sandbox_docker_image,
                memory=self.sandbox_memory,
                cpus=self.sandbox_cpus,
            )
            pytest_message = self.defender.review_pytest(last_sandbox)
            round_messages.append(
                AgentDebateMessage(
                    round=attempt,
                    agent="DEFENDER",
                    content=pytest_message,
                    metadata={
                        "returncode": last_sandbox.returncode,
                        "duration_seconds": last_sandbox.duration_seconds,
                    },
                )
            )
            if last_sandbox.passed:
                adversarial_critique = self.adversary.critique(current_code, request.issue_text)
                critique_message = _summarize_critique(adversarial_critique)
                round_messages.append(
                    AgentDebateMessage(
                        round=attempt,
                        agent="ADVERSARY",
                        content=critique_message,
                        metadata=adversarial_critique.model_dump(mode="json"),
                    )
                )
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="ADVERSARY_CRITIQUED",
                        agent="ADVERSARY",
                        message=critique_message,
                        metadata=adversarial_critique.model_dump(mode="json"),
                    ),
                )
                last_adversarial = self.adversary.generate_tests(
                    candidate_source=current_code,
                    workspace=workspace,
                    target_file=target_in_workspace,
                    issue_text=request.issue_text,
                    timeout_seconds=self.pytest_timeout_seconds,
                    backend=active_backend,
                    docker_image=self.sandbox_docker_image,
                    memory=self.sandbox_memory,
                    cpus=self.sandbox_cpus,
                )
                adversary_message = _summarize_adversary_pass(last_adversarial)
                round_messages.append(
                    AgentDebateMessage(
                        round=attempt,
                        agent="ADVERSARY",
                        content=adversary_message,
                        metadata={
                            "generated": last_adversarial.generated,
                            "passed": last_adversarial.passed,
                            "returncode": last_adversarial.returncode,
                        },
                    )
                )
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="ADVERSARY_CHALLENGED",
                        agent="ADVERSARY",
                        message=adversary_message,
                        metadata={
                            "generated": last_adversarial.generated,
                            "passed": last_adversarial.passed,
                            "returncode": last_adversarial.returncode,
                        },
                    ),
                )
                if not last_adversarial.passed:
                    graph_state = self._decide_graph(
                        attempt=attempt,
                        max_attempts=request.max_retry,
                        ast_ok=True,
                        pytest_passed=True,
                        adversarial_passed=False,
                        failure_feedback=_summarize_adversarial_failure(last_adversarial),
                    )
                    previous_error = (
                        _summarize_adversarial_failure(last_adversarial)
                        + "\n\n对抗 Agent 诊断：\n"
                        + critique_message
                    )
                    debate_rounds.append(
                        DebateRound(
                            round=attempt,
                            pytest_passed=True,
                            adversarial_passed=False,
                            code_change_percent=code_change_percent,
                            converged=False,
                            messages=round_messages,
                        )
                    )
                    append_trajectory(
                        trajectory_path,
                        TrajectoryStep(
                            attempt=attempt,
                            status="ADVERSARY_FAILED",
                            agent="ADVERSARY",
                            message=previous_error,
                            metadata={"graph": _graph_metadata(graph_state, self.graph_backend)},
                        ),
                    )
                    continue

                post = analyze_file(target_in_workspace)
                mutation_tests_path = _combined_mutation_tests_path(
                    workspace,
                    tests_in_workspace,
                    last_adversarial.test_file,
                )
                last_mutation = self.adversary.challenge(
                    candidate_source=current_code,
                    target_file=target_in_workspace,
                    workspace=workspace,
                    tests_path=mutation_tests_path,
                    timeout_seconds=self.pytest_timeout_seconds,
                    backend=active_backend,
                    docker_image=self.sandbox_docker_image,
                    memory=self.sandbox_memory,
                    cpus=self.sandbox_cpus,
                )
                mutation_message = _summarize_mutation(last_mutation)
                round_messages.append(
                    AgentDebateMessage(
                        round=attempt,
                        agent="ADVERSARY",
                        content=mutation_message,
                        metadata={
                            "total": last_mutation.total,
                            "killed": last_mutation.killed,
                            "survived": last_mutation.survived,
                            "kill_rate": last_mutation.kill_rate,
                        },
                    )
                )
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="ADVERSARY_CHALLENGED",
                        agent="ADVERSARY",
                        message=mutation_message,
                        metadata={
                            "total": last_mutation.total,
                            "killed": last_mutation.killed,
                            "survived": last_mutation.survived,
                            "kill_rate": last_mutation.kill_rate,
                        },
                    ),
                )
                last_performance = run_performance_profile_with_backend(
                    workspace=workspace,
                    target_file=target_in_workspace,
                    tests_path=tests_in_workspace,
                    timeout_seconds=self.pytest_timeout_seconds,
                    backend=active_backend,
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
                graph_state = self._decide_graph(
                    attempt=attempt,
                    max_attempts=request.max_retry,
                    ast_ok=True,
                    pytest_passed=True,
                    adversarial_passed=last_adversarial.passed,
                    mutation_kill_rate=last_mutation.kill_rate,
                    reward=reward.reward,
                )
                judge_message = _summarize_judge(reward)
                round_messages.append(
                    AgentDebateMessage(
                        round=attempt,
                        agent="JUDGE",
                        content=judge_message,
                        metadata={
                            **reward.model_dump(mode="json"),
                            "graph": _graph_metadata(graph_state, self.graph_backend),
                        },
                    )
                )
                debate_rounds.append(
                    DebateRound(
                        round=attempt,
                        candidate_loc=post.loc,
                        candidate_cc=post.cyclomatic_complexity,
                        pytest_passed=True,
                        adversarial_passed=last_adversarial.passed,
                        mutation_kill_rate=last_mutation.kill_rate,
                        reward=reward,
                        code_change_percent=code_change_percent,
                        converged=should_converge(attempt, code_change_percent, request.max_retry)
                        or last_adversarial.passed,
                        messages=round_messages,
                    )
                )
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="JUDGE_SCORED",
                        agent="JUDGE",
                        message=judge_message,
                        metadata={
                            **reward.model_dump(mode="json"),
                            "graph": _graph_metadata(graph_state, self.graph_backend),
                        },
                        reward=reward,
                    ),
                )
                if graph_state["verdict"] != "APPROVE":
                    previous_error = f"Judge verdict: {graph_state['verdict']}"
                    continue
                append_trajectory(
                    trajectory_path,
                    TrajectoryStep(
                        attempt=attempt,
                        status="DEBATE_CONVERGED",
                        agent="JUDGE",
                        message="极简候选代码扛过了防守检查、对抗测试、变异测试和裁判评分。",
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
                self.store.save_memory(success_memory(record, memory_key, llm_result.insult_review, reward))
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
                        debate_rounds,
                    ),
                    workspace_path=workspace,
                    attempts=attempt,
                    last_sandbox_result=last_sandbox,
                    candidate_file=target_in_workspace,
                    ast_validation=last_validation,
                    adversarial_result=last_adversarial,
                    mutation_result=last_mutation,
                    performance_profile=last_performance,
                    debate_rounds=debate_rounds,
                )

            previous_error = _summarize_failure(last_sandbox)
            graph_state = self._decide_graph(
                attempt=attempt,
                max_attempts=request.max_retry,
                ast_ok=True,
                pytest_passed=False,
                failure_feedback=previous_error,
            )
            debate_rounds.append(
                DebateRound(
                    round=attempt,
                    pytest_passed=False,
                    code_change_percent=code_change_percent,
                    converged=False,
                    messages=round_messages,
                )
            )
            append_trajectory(
                trajectory_path,
                TrajectoryStep(
                    attempt=attempt,
                    status="PYTEST_FAILED",
                    agent="DEFENDER",
                    message=previous_error,
                    metadata={
                        "returncode": last_sandbox.returncode,
                        "graph": _graph_metadata(graph_state, self.graph_backend),
                    },
                ),
            )

        record = RunRecord(
            run_id=run_id,
            issue_id=request.issue_id,
            repo_name=repo_name,
            pre_loc=baseline.loc,
            pre_cc=baseline.cyclomatic_complexity,
            self_heal_count=request.max_retry,
            status="FAILED",
            error=previous_error or "pytest 失败",
        )
        self.store.save(record)
        self.store.save_memory(failure_memory(record, memory_key))
        append_trajectory(
            trajectory_path,
            TrajectoryStep(attempt=request.max_retry, status="FAILED", message=record.error or "重构失败"),
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
                debate_rounds,
            ),
            workspace_path=workspace,
            attempts=request.max_retry,
            last_sandbox_result=last_sandbox,
            candidate_file=target_in_workspace,
            ast_validation=last_validation,
            adversarial_result=last_adversarial,
            mutation_result=last_mutation,
            performance_profile=last_performance,
            debate_rounds=debate_rounds,
        )


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"


def _graph_metadata(state: DebateGraphState, backend: str) -> dict[str, object]:
    return {
        "backend": backend,
        "node_trace": state["node_trace"],
        "verdict": state["verdict"],
        "failure_feedback": state["failure_feedback"],
    }


def _request_with_memory(request: RefactorRequest, memory_context: str | None) -> RefactorRequest:
    if not memory_context:
        return request
    return request.model_copy(
        update={
            "issue_text": (
                f"{request.issue_text}\n\n"
                "以下是系统从历史轨迹中提炼出的短期记忆，请把它当作额外约束：\n"
                f"{memory_context}"
            )
        }
    )


def _summarize_failure(result: SandboxResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return combined[-8000:] if combined else f"pytest 失败，返回码 {result.returncode}"


def _summarize_adversarial_failure(result: AdversarialTestResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return (
        "对抗 Agent 生成的测试失败：\n"
        + (combined[-8000:] if combined else f"pytest 失败，返回码 {result.returncode}")
    )


def _summarize_adversary_pass(result: AdversarialTestResult) -> str:
    if result.generated == 0:
        return "对抗 Agent 没找到可自动生成的规则边界测试，只能先把变异测试请上桌。"
    status = "通过" if result.passed else "失败"
    return f"对抗 Agent 生成 {result.generated} 个边界测试；候选代码结果：{status}。"


def _summarize_critique(critique: AdversarialCritique) -> str:
    plan = "; ".join(critique.attack_plan)
    hint = f" 反例提示：{critique.counterexample_hint}" if critique.counterexample_hint else ""
    return f"红队风险={critique.risk_level}。攻击计划：{plan or '暂无命中规则'}{hint}"


def _summarize_mutation(result: MutationTestResult) -> str:
    return (
        f"变异攻击击杀 {result.killed}/{result.total} 个变异体"
        f"（击杀率 {result.kill_rate * 100:.1f}%）。"
    )


def _summarize_judge(reward: RewardBreakdown) -> str:
    return (
        "裁判评分="
        f"{reward.reward:.2f}；LOC 改善={reward.delta_loc}；圈复杂度改善={reward.delta_cc}；"
        f"变异击杀率={reward.mutation_kill_rate:.2f}；重试次数={reward.retry_count}。"
    )


def _code_change_percent(before: str, after: str) -> float:
    return (1.0 - SequenceMatcher(None, before, after).ratio()) * 100


def _combined_mutation_tests_path(
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
    shutil.copy2(adversarial_test_file, adversarial_target / adversarial_test_file.name)
    return combined


def _build_report_legacy(
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
    debate_rounds: list[DebateRound] | None = None,
) -> str:
    loc_delta = _delta(record.pre_loc, record.post_loc)
    cc_delta = _delta(record.pre_cc, record.post_cc)
    lines = [
        "### 重构 Agent 毒舌报告 / Refactor Agent Report",
        "",
        f"- 状态 (Status): **{_status_cn(record.status)}**",
        f"- 运行 ID (Run ID): `{record.run_id}`",
        f"- 沙箱工作区 (Workspace): `{workspace}`",
        f"- 自愈次数 (Self-heal count): {record.self_heal_count}",
        f"- LOC: {record.pre_loc} -> {record.post_loc} ({loc_delta})",
        f"- 圈复杂度 (Cyclomatic Complexity): {record.pre_cc} -> {record.post_cc} ({cc_delta})",
    ]
    if sandbox_result is not None:
        lines.extend(
            [
                f"- Pytest 返回码 (Pytest return code): {sandbox_result.returncode}",
                f"- Pytest 耗时 (Pytest duration): {sandbox_result.duration_seconds:.2f}s",
            ]
        )
    if ast_validation is not None:
        lines.extend(["- AST 守卫 (AST guard): 通过" if ast_validation.ok else "- AST 守卫 (AST guard): 拒绝"])
    if adversarial_result is not None:
        lines.append(
            "- 对抗测试 (Adversarial tests): "
            f"生成 {adversarial_result.generated} 个，"
            f"{'通过' if adversarial_result.passed else '失败'}"
        )
    if mutation_result is not None:
        lines.append(
            "- 变异测试 (Mutation testing): "
            f"击杀 {mutation_result.killed}/{mutation_result.total} 个 "
            f"（击杀率 {mutation_result.kill_rate * 100:.1f}%）"
        )
    if performance_profile is not None:
        import_time = (
            f"{performance_profile.import_time_seconds:.4f}s"
            if performance_profile.import_time_seconds is not None
            else "n/a"
        )
        lines.extend(
            [
                f"- 性能采样 Pytest 耗时 (Profiled pytest duration): {performance_profile.pytest_duration_seconds:.2f}s",
                f"- 峰值追踪内存 (Peak traced memory): {performance_profile.peak_memory_kib:.1f} KiB",
                f"- 模块导入耗时 (Module import time): {import_time}",
            ]
        )
    if reward is not None:
        lines.append(f"- 裁判奖励分 (Reward): {reward.reward:.2f}")
    if debate_rounds:
        converged = sum(1 for item in debate_rounds if item.converged)
        lines.append(f"- 多 Agent 对抗轮次 (Multi-agent debate rounds): {len(debate_rounds)}（{converged} 轮收敛）")
        lines.extend(["", "#### 对抗状态机 (Debate State Machine)", "", "```mermaid", render_mermaid_state_diagram(), "```"])
        lines.extend(["", "#### 多 Agent 对抗记录 (Multi-Agent Debate)", ""])
        for item in debate_rounds:
            lines.append(
                f"- 第 {item.round} 轮: pytest={_bool_cn(item.pytest_passed)}, "
                f"对抗={_bool_cn(item.adversarial_passed)}, "
                f"变异击杀率={_format_optional_rate(item.mutation_kill_rate)}, "
                f"奖励分={_format_optional_reward(item.reward)}"
            )
            for message in item.messages:
                lines.append(f"  - {message.agent}: {message.content}")
    if mutation_result and mutation_result.survival_details:
        lines.extend(["", "#### 未被杀死的变异体 (Surviving Mutants)", ""])
        lines.extend(f"- {detail}" for detail in mutation_result.survival_details)
    if review:
        lines.extend(["", "#### 毒舌代码审查 (Code Review)", "", review])
    if error:
        lines.extend(["", "#### 错误详情 (Error)", "", "```text", error[-4000:], "```"])
    return "\n".join(lines)


def _delta(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    change = after - before
    if before == 0:
        return f"{change:+d}"
    percentage = (change / before) * 100
    return f"{change:+d}, {percentage:+.1f}%"


def _status_cn(status: str) -> str:
    return {"SUCCESS": "成功", "FAILED": "失败"}.get(status, status)


def _bool_cn(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "通过" if value else "失败"


def _format_optional_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _format_optional_reward(value: RewardBreakdown | None) -> str:
    if value is None:
        return "n/a"
    return f"{value.reward:.2f}"


def _build_report(
    record: RunRecord,
    workspace: Path,
    review: str | None,
    sandbox_result: SandboxResult | None,
    error: str | None,
    ast_validation: CandidateValidationResult | None = None,
    adversarial_result: AdversarialTestResult | None = None,
    mutation_result: MutationTestResult | None = None,
    reward: RewardBreakdown | None = None,
    performance_profile: PerformanceProfile | None = None,
    debate_rounds: list[DebateRound] | None = None,
) -> str:
    loc_delta = _delta(record.pre_loc, record.post_loc)
    cc_delta = _delta(record.pre_cc, record.post_cc)
    pytest_status = _report_bool_status(sandbox_result.passed if sandbox_result else None)
    ast_status = _report_bool_status(ast_validation.ok if ast_validation else None)
    adversarial_status = _report_bool_status(adversarial_result.passed if adversarial_result else None)
    mutation_status = _report_mutation_status(mutation_result)
    performance_status = _report_bool_status(performance_profile.passed if performance_profile else None)
    import_time = (
        f"{performance_profile.import_time_seconds:.4f}s"
        if performance_profile and performance_profile.import_time_seconds is not None
        else "n/a"
    )
    lines = [
        "### 重构 Agent 毒舌报告 / Refactor Agent Report",
        "",
        "#### 结论摘要",
        "",
        f"- 状态 (Status): **{_report_status_cn(record.status)}**",
        f"- 运行 ID (Run ID): `{record.run_id}`",
        f"- 沙箱工作区 (Workspace): `{workspace}`",
        f"- 毒舌结论: {_report_verdict(record, mutation_result, reward)}",
        "",
        "#### 指标对比表",
        "",
    ]
    lines.extend(
        _report_markdown_table(
            ["维度", "重构前", "重构后", "差值/结果"],
            [
                ["LOC", record.pre_loc, record.post_loc, loc_delta],
                ["Cyclomatic Complexity", record.pre_cc, record.post_cc, cc_delta],
                ["Self-heal count", "n/a", record.self_heal_count, f"{record.self_heal_count} 次"],
                [
                    "Pytest return code",
                    "n/a",
                    sandbox_result.returncode if sandbox_result else None,
                    pytest_status,
                ],
                [
                    "Pytest duration",
                    "n/a",
                    _report_seconds(sandbox_result.duration_seconds if sandbox_result else None),
                    pytest_status,
                ],
                [
                    "Profiled pytest duration",
                    "n/a",
                    _report_seconds(performance_profile.pytest_duration_seconds if performance_profile else None),
                    performance_status,
                ],
                [
                    "Peak traced memory",
                    "n/a",
                    _report_kib(performance_profile.peak_memory_kib if performance_profile else None),
                    performance_status,
                ],
                ["Module import time", "n/a", import_time, performance_status],
                ["Reward", "n/a", _format_optional_reward(reward), _report_reward_comment(reward)],
            ],
        )
    )
    lines.extend(["", "#### 验证矩阵", ""])
    lines.extend(
        _report_markdown_table(
            ["检查项", "状态", "证据"],
            [
                ["AST 守卫 (AST guard)", ast_status, _report_ast_evidence(ast_validation)],
                ["基线测试 (Pytest)", pytest_status, _report_pytest_evidence(sandbox_result)],
                ["对抗测试 (Adversarial tests)", adversarial_status, _report_adversarial_evidence(adversarial_result)],
                ["变异测试 (Mutation testing)", mutation_status, _report_mutation_evidence(mutation_result)],
                ["性能采样 (Performance profiling)", performance_status, _report_performance_evidence(performance_profile)],
            ],
        )
    )
    lines.extend(
        [
            "",
            "#### 兼容摘要",
            "",
            f"- 自愈次数 (Self-heal count): {record.self_heal_count}",
            f"- LOC: {record.pre_loc} -> {record.post_loc} ({loc_delta})",
            f"- 圈复杂度 (Cyclomatic Complexity): {record.pre_cc} -> {record.post_cc} ({cc_delta})",
            f"- Pytest 返回码 (Pytest return code): {sandbox_result.returncode if sandbox_result else 'n/a'}",
            f"- Pytest 耗时 (Pytest duration): {_report_seconds(sandbox_result.duration_seconds if sandbox_result else None)}",
            f"- AST 守卫 (AST guard): {ast_status}",
            f"- 对抗测试 (Adversarial tests): {_report_adversarial_evidence(adversarial_result)}",
            f"- 变异测试 (Mutation testing): {_report_mutation_evidence(mutation_result)}",
            f"- 性能采样 Pytest 耗时 (Profiled pytest duration): "
            f"{_report_seconds(performance_profile.pytest_duration_seconds if performance_profile else None)}",
            f"- 峰值追踪内存 (Peak traced memory): "
            f"{_report_kib(performance_profile.peak_memory_kib if performance_profile else None)}",
            f"- 模块导入耗时 (Module import time): {import_time}",
            f"- 裁判奖励分 (Reward): {_format_optional_reward(reward)}",
        ]
    )
    if debate_rounds:
        converged = sum(1 for item in debate_rounds if item.converged)
        lines.append(f"- 多 Agent 对抗轮次 (Multi-agent debate rounds): {len(debate_rounds)}（{converged} 轮收敛）")
        lines.extend(["", "#### 对抗状态机 (Debate State Machine)", "", "```mermaid", render_mermaid_state_diagram(), "```"])
        lines.extend(["", "#### 多 Agent 对抗记录 (Multi-Agent Debate)", ""])
        for item in debate_rounds:
            lines.append(
                f"- 第 {item.round} 轮: pytest={_report_bool_status(item.pytest_passed)}, "
                f"对抗={_report_bool_status(item.adversarial_passed)}, "
                f"变异击杀率={_format_optional_rate(item.mutation_kill_rate)}, "
                f"奖励分={_format_optional_reward(item.reward)}"
            )
            for message in item.messages:
                lines.append(f"  - {message.agent}: {message.content}")
    if mutation_result and mutation_result.survival_details:
        lines.extend(["", "#### 未被杀死的变异体 (Surviving Mutants)", ""])
        lines.extend(f"- {detail}" for detail in mutation_result.survival_details)
    if review:
        lines.extend(["", "#### 毒舌代码审查 (Code Review)", "", review])
    if error:
        lines.extend(["", "#### 错误详情 (Error)", "", "```text", error[-4000:], "```"])
    return "\n".join(lines)


def _report_markdown_table(headers: list[object], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(_report_md_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_report_md_cell(cell) for cell in row) + " |" for row in rows)
    return lines


def _report_md_cell(value: object) -> str:
    text = "n/a" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    if len(text) > 360:
        return f"{text[:357]}..."
    return text


def _report_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}s"


def _report_kib(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} KiB"


def _report_bool_status(value: bool | None) -> str:
    if value is None:
        return "未执行"
    return "通过" if value else "失败"


def _report_mutation_status(result: MutationTestResult | None) -> str:
    if result is None:
        return "未执行"
    if result.total == 0:
        return "无变异体"
    return "通过" if result.survived == 0 else "有漏网变异体"


def _report_status_cn(status: str) -> str:
    return {"SUCCESS": "成功", "FAILED": "失败"}.get(status, status)


def _report_ast_evidence(result: CandidateValidationResult | None) -> str:
    if result is None:
        return "未执行 AST 结构守卫。"
    if result.ok:
        if result.analysis:
            return (
                f"LOC={result.analysis.loc}, CC={result.analysis.cyclomatic_complexity}, "
                f"public_symbols={len(result.analysis.public_symbols)}"
            )
        return "候选代码通过结构与安全检查。"
    summary = result.summary()
    return summary or "AST 守卫拒绝候选代码。"


def _report_pytest_evidence(result: SandboxResult | None) -> str:
    if result is None:
        return "未运行 pytest。"
    return f"returncode={result.returncode}, duration={result.duration_seconds:.2f}s"


def _report_adversarial_evidence(result: AdversarialTestResult | None) -> str:
    if result is None:
        return "未生成对抗测试。"
    status = "通过" if result.passed else "失败"
    return f"生成 {result.generated} 个边界测试，returncode={result.returncode}，结果={status}"


def _report_mutation_evidence(result: MutationTestResult | None) -> str:
    if result is None:
        return "未运行变异测试。"
    return (
        f"击杀 {result.killed}/{result.total} 个变异体，"
        f"击杀率 {result.kill_rate * 100:.1f}%，存活 {result.survived} 个"
    )


def _report_performance_evidence(result: PerformanceProfile | None) -> str:
    if result is None:
        return "未运行性能采样。"
    import_time = f"{result.import_time_seconds:.4f}s" if result.import_time_seconds is not None else "n/a"
    return (
        f"returncode={result.pytest_returncode}, "
        f"Profiled pytest duration={result.pytest_duration_seconds:.2f}s, "
        f"Peak traced memory={result.peak_memory_kib:.1f} KiB, "
        f"Module import time={import_time}"
    )


def _report_reward_comment(reward: RewardBreakdown | None) -> str:
    if reward is None:
        return "未评分"
    if reward.reward >= 1:
        return "值得合并"
    if reward.reward >= 0:
        return "勉强能看"
    return "还得回炉"


def _report_verdict(
    record: RunRecord,
    mutation_result: MutationTestResult | None,
    reward: RewardBreakdown | None,
) -> str:
    if record.status == "FAILED":
        return "这轮修复没能过关，代码还在测试门口原地罚站。"
    if mutation_result and mutation_result.survived:
        return "主线测试过了，但变异体还活着，说明测试网眼大得能漏掉逻辑事故。"
    if reward and reward.reward >= 1:
        return "这次终于像重构了：更短、更稳，还没把行为顺手掀翻。"
    return "功能跑通了，复杂度也收住了，旧代码那点绕路癖好被按回去了。"
