from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from uuid import uuid4

from refactor_agent.agents import AdversaryAgent, DefenderAgent, JudgeAgent, MinimizerAgent
from refactor_agent.artifacts import RunArtifactWriter
from refactor_agent.ast_analyzer import controlled_subtree_rewrite, select_target_regions, validate_candidate_source
from refactor_agent.execution_graph import ExecutionState, run_execution_graph
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.debate_state import render_mermaid_state_diagram
from refactor_agent.llm import LLMError, RefactorClient
from refactor_agent.memory import build_memory_context, failure_memory, success_memory, target_memory_key
from refactor_agent.metrics import analyze_file
from refactor_agent.models import (
    AdversarialCritique,
    AdversarialTestResult,
    AgentDebateMessage,
    AstRewriteResult,
    CandidateValidationResult,
    DebateRound,
    EvidenceLevel,
    LLMUsage,
    MutationTestResult,
    PerformanceProfile,
    RefactorRequest,
    RefactorRunResult,
    RewardBreakdown,
    ReportPersona,
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

    def run(
        self,
        request: RefactorRequest,
        execution_control: ExecutionControl | None = None,
    ) -> RefactorRunResult:
        control = execution_control or ExecutionControl(
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=900)
        )
        return _RefactorWorkflow(self, request, control).run()


class _RefactorWorkflow:
    def __init__(
        self,
        orchestrator: RefactorOrchestrator,
        request: RefactorRequest,
        execution_control: ExecutionControl,
    ) -> None:
        self.orchestrator = orchestrator
        self.request = request
        self.run_id = _new_run_id()
        self.workspace = orchestrator.run_root / self.run_id / "workspace"
        self.repo_name = request.repo_name or request.target_file.resolve().parent.name
        self.memory_key = target_memory_key(request.target_file)
        self.trajectory_path = orchestrator.run_root / self.run_id / "trajectory.jsonl"
        self.execution_control = execution_control

    def run(self) -> RefactorRunResult:
        final = run_execution_graph(
            {
                "attempt": 0,
                "max_attempts": self.request.max_retry,
                "current_code": "",
                "previous_error": None,
                "debate_rounds": [],
                "llm_usages": [],
                "node_trace": [],
                "next_node": "prepare",
            },
            self,
            self.orchestrator.graph_backend,
            execution_control=self.execution_control,
        )
        result: RefactorRunResult = final["result"]
        result.graph_backend = self.orchestrator.graph_backend
        result.graph_node_trace = final["node_trace"]
        return result

    def prepare(self, state: ExecutionState) -> ExecutionState:
        memory = build_memory_context(self.orchestrator.store.list_memory(self.repo_name, self.memory_key, limit=3))
        state["llm_request"] = _request_with_memory(self.request, memory)
        state["baseline"] = analyze_file(self.request.target_file)
        state["original_code"] = self.request.target_file.read_text(encoding="utf-8")
        state["current_code"] = state["original_code"]
        _, state["target_file"], state["tests_path"] = prepare_workspace(
            self.request.target_file,
            self.request.tests_path,
            self.workspace,
        )
        try:
            state["active_backend"], _ = resolve_sandbox_backend(self.orchestrator.sandbox_backend)
        except SandboxUnavailableError as exc:
            state["terminal_error"] = str(exc)
            state["next_node"] = "finalize"
            return state
        state["next_node"] = "minimizer"
        return state

    def minimizer(self, state: ExecutionState) -> ExecutionState:
        state["attempt"] += 1
        state["allowed_regions"] = select_target_regions(
            state["original_code"],
            self.request.issue_text,
            state.get("previous_error"),
        )
        try:
            result = self.orchestrator.minimizer.propose(
                request=state["llm_request"],
                current_code=state["current_code"],
                baseline_metrics=state["baseline"],
                previous_error=state.get("previous_error"),
                attempt=state["attempt"],
            )
        except LLMError as exc:
            state["terminal_error"] = str(exc)
            state["next_node"] = "finalize"
            return state
        state["llm_result"] = result
        if result.usage is not None:
            state["llm_usages"] = [*state.get("llm_usages", []), result.usage]
        state["round_messages"] = [
            AgentDebateMessage(round=state["attempt"], agent="MINIMIZER", content=result.thought)
        ]
        self._trajectory(state, "MINIMIZER_PROPOSED", result.thought, "MINIMIZER")
        state["next_node"] = "ast_guard"
        return state

    def ast_guard(self, state: ExecutionState) -> ExecutionState:
        rewrite = controlled_subtree_rewrite(
            state["original_code"],
            state["llm_result"].fixed_code,
            state["allowed_regions"],
            self.request.allowed_import_roots,
        )
        state["rewrite"] = rewrite
        candidate = rewrite.source
        state["code_change_percent"] = _code_change_percent(
            state.get("previous_candidate_code") or state["original_code"], candidate
        )
        state["previous_candidate_code"] = candidate
        state["current_code"] = candidate
        validation = validate_candidate_source(state["original_code"], candidate)
        if not rewrite.ok:
            validation = CandidateValidationResult(ok=False, findings=rewrite.findings)
        state["validation"] = validation
        message = self.orchestrator.defender.review_static(validation)
        state["round_messages"].append(
            AgentDebateMessage(round=state["attempt"], agent="DEFENDER", content=message)
        )
        if not validation.ok:
            state["previous_error"] = "AST guard rejected candidate:\n" + validation.summary()
            self._trajectory(
                state,
                "AST_REJECTED",
                state["previous_error"],
                "DEFENDER",
                self._rewrite_metadata(rewrite),
            )
            self._close_round(state)
            return self._retry_or_finalize(state)
        self._trajectory(
            state,
            "DEFENDER_REVIEWED",
            message,
            "DEFENDER",
            self._rewrite_metadata(rewrite),
        )
        state["next_node"] = "pytest"
        return state

    def pytest(self, state: ExecutionState) -> ExecutionState:
        write_candidate(state["target_file"], state["current_code"])
        result = run_pytest_with_backend(
            workspace=self.workspace,
            tests_path=state["tests_path"],
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            backend=state["active_backend"],
            docker_image=self.orchestrator.sandbox_docker_image,
            memory=self.orchestrator.sandbox_memory,
            cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
        )
        state["sandbox"] = result
        message = self.orchestrator.defender.review_pytest(result)
        state["round_messages"].append(
            AgentDebateMessage(round=state["attempt"], agent="DEFENDER", content=message)
        )
        if not result.passed:
            state["previous_error"] = _summarize_failure(result)
            self._trajectory(state, "PYTEST_FAILED", state["previous_error"], "DEFENDER")
            self._close_round(state, pytest_passed=False)
            return self._retry_or_finalize(state)
        state["next_node"] = "adversary"
        return state

    def adversary(self, state: ExecutionState) -> ExecutionState:
        critique = self.orchestrator.adversary.critique(state["current_code"], self.request.issue_text)
        critique_message = _summarize_critique(critique)
        state["round_messages"].append(
            AgentDebateMessage(round=state["attempt"], agent="ADVERSARY", content=critique_message)
        )
        self._trajectory(state, "ADVERSARY_CRITIQUED", critique_message, "ADVERSARY")
        result = self.orchestrator.adversary.generate_tests(
            candidate_source=state["current_code"],
            workspace=self.workspace,
            target_file=state["target_file"],
            issue_text=self.request.issue_text,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            backend=state["active_backend"],
            docker_image=self.orchestrator.sandbox_docker_image,
            memory=self.orchestrator.sandbox_memory,
            cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
        )
        state["adversarial"] = result
        message = _summarize_adversary_pass(result)
        state["round_messages"].append(
            AgentDebateMessage(round=state["attempt"], agent="ADVERSARY", content=message)
        )
        self._trajectory(state, "ADVERSARY_CHALLENGED", message, "ADVERSARY")
        if not result.passed:
            state["previous_error"] = _summarize_adversarial_failure(result) + "\n" + critique_message
            self._trajectory(state, "ADVERSARY_FAILED", state["previous_error"], "ADVERSARY")
            self._close_round(state, pytest_passed=True, adversarial_passed=False)
            return self._retry_or_finalize(state)
        state["next_node"] = "mutation"
        return state

    def mutation(self, state: ExecutionState) -> ExecutionState:
        state["post"] = analyze_file(state["target_file"])
        mutation_tests = _combined_mutation_tests_path(
            self.workspace,
            state["tests_path"],
            state["adversarial"].test_file,
        )
        state["mutation"] = self.orchestrator.adversary.challenge(
            candidate_source=state["current_code"],
            target_file=state["target_file"],
            workspace=self.workspace,
            tests_path=mutation_tests,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            backend=state["active_backend"],
            docker_image=self.orchestrator.sandbox_docker_image,
            memory=self.orchestrator.sandbox_memory,
            cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
        )
        message = _summarize_mutation(state["mutation"])
        state["round_messages"].append(
            AgentDebateMessage(round=state["attempt"], agent="ADVERSARY", content=message)
        )
        self._trajectory(state, "ADVERSARY_CHALLENGED", message, "ADVERSARY")
        state["performance"] = run_performance_profile_with_backend(
            workspace=self.workspace,
            target_file=state["target_file"],
            tests_path=state["tests_path"],
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            backend=state["active_backend"],
            docker_image=self.orchestrator.sandbox_docker_image,
            memory=self.orchestrator.sandbox_memory,
            cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
        )
        state["next_node"] = "judge"
        return state

    def judge(self, state: ExecutionState) -> ExecutionState:
        reward = self.orchestrator.judge.score(
            pre=state["baseline"],
            post=state["post"],
            retry_count=state["attempt"] - 1,
            mutation_result=state["mutation"],
            adversarial_result=state["adversarial"],
        )
        state["reward"] = reward
        approved = state["adversarial"].passed and state["mutation"].kill_rate >= 1.0
        verdict = "APPROVE" if approved else ("RETRY" if state["attempt"] < state["max_attempts"] else "REJECT")
        message = _summarize_judge(reward)
        graph = {
            "backend": self.orchestrator.graph_backend,
            "node_trace": [*state.get("node_trace", []), "JUDGE"],
            "verdict": verdict,
        }
        state["round_messages"].append(
            AgentDebateMessage(round=state["attempt"], agent="JUDGE", content=message, metadata={"graph": graph})
        )
        self._close_round(
            state,
            pytest_passed=True,
            adversarial_passed=state["adversarial"].passed,
            mutation_kill_rate=state["mutation"].kill_rate,
            reward=reward,
            converged=approved,
        )
        self._trajectory(state, "JUDGE_SCORED", message, "JUDGE", {"graph": graph}, reward)
        if approved:
            state["approved"] = True
            self._trajectory(state, "DEBATE_CONVERGED", "Candidate passed the executed graph.", "JUDGE", reward=reward)
            state["next_node"] = "finalize"
            return state
        survivors = "; ".join(state["mutation"].survival_details) or "none"
        state["previous_error"] = (
            f"Judge verdict: {verdict}. Mutation kill rate: {state['mutation'].kill_rate:.3f}. "
            f"Surviving mutants: {survivors}"
        )
        return self._retry_or_finalize(state)

    def finalize(self, state: ExecutionState) -> ExecutionState:
        baseline = state.get("baseline")
        approved = bool(state.get("approved"))
        error = None if approved else str(state.get("terminal_error") or state.get("previous_error") or "refactor failed")
        post = state.get("post") if approved else None
        attempts = int(state.get("attempt", 0))
        if approved or state.get("terminal_error"):
            self_heal_count = max(attempts - 1, 0)
        else:
            self_heal_count = attempts
        graph_trace = [*state.get("node_trace", []), "FINALIZE"]
        record = RunRecord(
            run_id=self.run_id,
            issue_id=self.request.issue_id,
            repo_name=self.repo_name,
            pre_loc=baseline.loc if baseline else None,
            post_loc=post.loc if post else None,
            pre_cc=baseline.cyclomatic_complexity if baseline else None,
            post_cc=post.cyclomatic_complexity if post else None,
            self_heal_count=self_heal_count,
            status="SUCCESS" if approved else "FAILED",
            error=error,
            evidence_level=self.request.evidence_level,
            report_persona=self.request.persona,
        )
        self.orchestrator.store.save(record)
        llm_result = state.get("llm_result")
        if approved:
            self.orchestrator.store.save_memory(
                success_memory(record, self.memory_key, llm_result.insult_review, state["reward"])
            )
        else:
            self.orchestrator.store.save_memory(failure_memory(record, self.memory_key))
            self._trajectory(
                state,
                str(state.get("control_status") or "FAILED"),
                error or "refactor failed",
            )
        report = _build_report(
            record,
            self.workspace,
            llm_result.insult_review if approved and llm_result else None,
            state.get("sandbox"),
            error,
            state.get("validation"),
            state.get("adversarial"),
            state.get("mutation"),
            state.get("reward"),
            state.get("performance"),
            state["debate_rounds"],
            state.get("rewrite"),
            self.orchestrator.graph_backend,
            graph_trace,
            self.request.evidence_level,
            self.request.persona,
            llm_usages=state.get("llm_usages", []),
        )
        self._write_artifacts(state, report)
        state["result"] = RefactorRunResult(
            record=record,
            report_markdown=report,
            workspace_path=self.workspace,
            attempts=attempts,
            last_sandbox_result=state.get("sandbox"),
            candidate_file=state.get("target_file"),
            ast_validation=state.get("validation"),
            ast_rewrite=state.get("rewrite"),
            adversarial_result=state.get("adversarial"),
            mutation_result=state.get("mutation"),
            performance_profile=state.get("performance"),
            debate_rounds=state["debate_rounds"],
            graph_backend=self.orchestrator.graph_backend,
            graph_node_trace=graph_trace,
            llm_usages=state.get("llm_usages", []),
            evidence_level=self.request.evidence_level,
            report_persona=self.request.persona,
        )
        state["next_node"] = "finalize"
        return state

    def _write_artifacts(self, state: ExecutionState, report: str) -> None:
        writer = RunArtifactWriter(self.orchestrator.run_root / self.run_id)
        original = str(state.get("original_code") or "")
        candidate = str(state.get("current_code") or original)
        writer.write_sources(original, candidate)
        sandbox = state.get("sandbox")
        writer.write_log(
            "pytest.log",
            "\n".join(part for part in [getattr(sandbox, "stdout", ""), getattr(sandbox, "stderr", "")] if part),
        )
        adversarial = state.get("adversarial")
        writer.write_log(
            "adversary.log",
            "\n".join(
                part
                for part in [getattr(adversarial, "stdout", ""), getattr(adversarial, "stderr", "")]
                if part
            ),
        )
        mutation = state.get("mutation")
        writer.write_json("mutation.json", mutation.model_dump(mode="json") if mutation else {})
        writer.write_report(report)

    def _retry_or_finalize(self, state: ExecutionState) -> ExecutionState:
        state["next_node"] = "minimizer" if state["attempt"] < state["max_attempts"] else "finalize"
        return state

    def _close_round(self, state: ExecutionState, **updates) -> None:
        state["debate_rounds"].append(
            DebateRound(
                round=state["attempt"],
                code_change_percent=state.get("code_change_percent"),
                messages=state.get("round_messages", []),
                **updates,
            )
        )

    def _trajectory(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
        metadata: dict | None = None,
        reward: RewardBreakdown | None = None,
    ) -> None:
        append_trajectory(
            self.trajectory_path,
            TrajectoryStep(
                attempt=int(state.get("attempt", 0)),
                status=status,
                message=message,
                agent=agent,
                metadata=metadata or {},
                reward=reward,
            ),
        )

    @staticmethod
    def _rewrite_metadata(rewrite: AstRewriteResult) -> dict[str, object]:
        return {
            "selected_targets": [region.model_dump(mode="json") for region in rewrite.selected_regions],
            "changed_regions": rewrite.changed_regions,
            "added_imports": rewrite.added_imports,
        }


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"


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

def _delta(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    change = after - before
    if before == 0:
        return f"{change:+d}"
    percentage = (change / before) * 100
    return f"{change:+d}, {percentage:+.1f}%"


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
    ast_rewrite: AstRewriteResult | None = None,
    graph_backend: str | None = None,
    graph_node_trace: list[str] | None = None,
    evidence_level: EvidenceLevel = EvidenceLevel.REPOSITORY_TESTS,
    report_persona: ReportPersona = ReportPersona.STRICT,
    llm_usages: list[LLMUsage] | None = None,
) -> str:
    technical = _build_technical_report(
        record,
        workspace,
        review,
        sandbox_result,
        error,
        ast_validation,
        adversarial_result,
        mutation_result,
        reward,
        performance_profile,
        debate_rounds,
        ast_rewrite,
        graph_backend,
        graph_node_trace,
        evidence_level,
        report_persona,
    )
    decision, next_action = _report_decision(evidence_level, record.status)
    provider = _report_llm_usage(llm_usages or [])
    loc_delta = _delta(record.pre_loc, record.post_loc)
    cc_delta = _delta(record.pre_cc, record.post_cc)
    summary = [
        "# Code Judge Report",
        "",
        f"> **Decision: {decision}** | Evidence: **{evidence_level.value}** | Persona: **{report_persona.value}**",
        "",
        "## Decision",
        "",
        *_report_markdown_table(
            ["Field", "Value"],
            [
                ["Run", f"`{record.run_id}`"],
                ["Status", _report_status_cn(record.status)],
                ["Provider / model", provider],
                ["LOC", f"{record.pre_loc} -> {record.post_loc} ({loc_delta})"],
                ["Cyclomatic complexity", f"{record.pre_cc} -> {record.post_cc} ({cc_delta})"],
            ],
        ),
        "",
        f"**Next action:** {next_action}",
        "",
        "## Evidence Summary",
        "",
        f"- {_evidence_boundary(evidence_level)}",
        f"- Pytest: {_report_bool_status(sandbox_result.passed if sandbox_result else None)}; "
        f"Adversarial: {_report_bool_status(adversarial_result.passed if adversarial_result else None)}; "
        f"Mutation: {_report_mutation_status(mutation_result)}",
        "",
        "<details>",
        "<summary>Technical appendix (full metrics, AST, graph, and debate)</summary>",
        "",
        technical,
        "",
        "</details>",
    ]
    return "\n".join(summary)


def _build_technical_report(
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
    ast_rewrite: AstRewriteResult | None = None,
    graph_backend: str | None = None,
    graph_node_trace: list[str] | None = None,
    evidence_level: EvidenceLevel = EvidenceLevel.REPOSITORY_TESTS,
    report_persona: ReportPersona = ReportPersona.STRICT,
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
        f"- 证据等级 (Evidence Level): **{evidence_level.value}**",
        f"- 报告人格 (Persona): **{report_persona.value}**",
        f"- 沙箱工作区 (Workspace): `{workspace}`",
        f"- 毒舌结论: {_report_verdict(record, mutation_result, reward)}",
        "",
        "#### 证据边界",
        "",
        f"- {_evidence_boundary(evidence_level)}",
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
    if ast_rewrite is not None:
        targets = ", ".join(
            f"{region.qualified_name} ({region.reason})" for region in ast_rewrite.selected_regions
        ) or ", ".join(ast_rewrite.allowed_regions) or "none"
        changed = ", ".join(ast_rewrite.changed_regions) or "none"
        imports = ", ".join(ast_rewrite.added_imports) or "none"
        lines.extend(
            [
                "",
                "#### Controlled AST Rewrite",
                "",
                f"- Selected AST targets: {targets}",
                f"- Changed AST regions: {changed}",
                f"- Added imports: {imports}",
            ]
        )
    if graph_backend and graph_node_trace:
        lines.extend(
            [
                "",
                "#### Execution Graph",
                "",
                f"- Graph backend: {graph_backend}",
                f"- Executed graph nodes: {' -> '.join(graph_node_trace)}",
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


def _evidence_boundary(level: EvidenceLevel) -> str:
    return {
        EvidenceLevel.STATIC: "仅有静态分析和导入 smoke；候选没有获得行为验证。",
        EvidenceLevel.GENERATED_TESTS: "候选只通过系统自动推导的测试，不能等同用户或仓库回归测试。",
        EvidenceLevel.USER_TESTS: "候选通过用户提供的 pytest 与自动攻击测试。",
        EvidenceLevel.REPOSITORY_TESTS: "候选通过仓库测试与自动攻击测试。",
    }[level]


def _report_decision(evidence: EvidenceLevel, status: str) -> tuple[str, str]:
    if status != "SUCCESS":
        return "DO NOT ADOPT", "修复未通过裁决；先查看错误详情和失败证据。"
    if evidence == EvidenceLevel.STATIC:
        return "REVIEW ONLY", "补充用户或仓库测试后再考虑采用候选。"
    if evidence == EvidenceLevel.GENERATED_TESTS:
        return "CONDITIONAL", "先审阅自动生成测试；它不能替代用户或仓库回归测试。"
    return "ADOPT WITH EVIDENCE", "查看 diff 后合并候选，并保留本次验证产物。"


def _report_llm_usage(usages: list[LLMUsage]) -> str:
    if not usages:
        return "n/a (no model call recorded)"
    providers = sorted({f"{item.provider}/{item.model}" for item in usages})
    total_tokens = sum(item.total_tokens or 0 for item in usages)
    costs = [item.cost_usd for item in usages if item.cost_usd is not None]
    suffix = f", {total_tokens} tokens" if total_tokens else ""
    if costs:
        suffix += f", ${sum(costs):.4f}"
    return "; ".join(providers) + suffix


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
