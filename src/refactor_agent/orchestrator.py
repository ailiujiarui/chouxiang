from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.analysis_events import AnalysisEventSink, AnalysisEventType
from refactor_agent.agents import AdversaryAgent, DefenderAgent, JudgeAgent, MinimizerAgent
from refactor_agent.execution_graph import ExecutionState, run_execution_graph
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.debate_state import render_mermaid_state_diagram
from refactor_agent.llm import RefactorClient
from refactor_agent.memory import target_memory_key
from refactor_agent.models import (
    AdversarialCritique,
    AdversarialTestResult,
    AgentDebateMessage,
    AstRewriteResult,
    CandidateValidationResult,
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
)
from refactor_agent.orchestrator_artifacts import write_run_artifacts
from refactor_agent.orchestrator_adversary import (
    run_adversary_execution_node,
    summarize_adversarial_failure,
    summarize_adversary_pass,
    summarize_critique,
)
from refactor_agent.orchestrator_ast_guard import (
    code_change_percent,
    guard_ast_execution_node,
    rewrite_metadata,
)
from refactor_agent.orchestrator_observability import OrchestratorObservability
from refactor_agent.orchestrator_persistence import persist_run_outcome
from refactor_agent.orchestrator_minimizer import minimize_execution_node
from refactor_agent.orchestrator_mutation import (
    combined_mutation_tests_path,
    run_mutation_execution_node,
    summarize_mutation,
)
from refactor_agent.orchestrator_prepare import (
    prepare_execution_node,
    request_with_memory as prepare_request_with_memory,
)
from refactor_agent.orchestrator_pytest import (
    run_pytest_execution_node,
    summarize_pytest_failure,
)
from refactor_agent.orchestrator_state import (
    WorkflowNode,
    close_debate_round,
    initial_execution_state,
    retry_or_finalize,
    transition_to,
)
from refactor_agent.store import SQLiteRunStore


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
        analysis_event_sink: AnalysisEventSink | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.run_root = run_root.resolve()
        self.store = store or SQLiteRunStore(self.run_root / "refactor_agent.sqlite")
        self.analysis_event_sink = analysis_event_sink or self.store
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
        self.observability = OrchestratorObservability(
            trajectory_path=self.trajectory_path,
            analysis_event_sink=orchestrator.analysis_event_sink,
            task_id=request.issue_id or self.run_id,
            run_id=self.run_id,
            evidence_level=request.evidence_level.value,
        )
        self.execution_control = execution_control

    def run(self) -> RefactorRunResult:
        final = run_execution_graph(
            initial_execution_state(self.request.max_retry),
            self,
            self.orchestrator.graph_backend,
            execution_control=self.execution_control,
        )
        result: RefactorRunResult = final["result"]
        result.graph_backend = self.orchestrator.graph_backend
        result.graph_node_trace = final["node_trace"]
        return result

    def prepare(self, state: ExecutionState) -> ExecutionState:
        """Prepare the run and convert sandbox startup details to a sanitized terminal error."""

        self._phase_started(state, "prepare")
        return prepare_execution_node(
            state,
            request=self.request,
            workspace=self.workspace,
            store=self.orchestrator.store,
            repo_name=self.repo_name,
            memory_key=self.memory_key,
            sandbox_backend=self.orchestrator.sandbox_backend,
        )

    def minimizer(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "minimizer")
        return minimize_execution_node(
            state,
            request=self.request,
            minimizer=self.orchestrator.minimizer,
            record_trajectory=self._trajectory,
        )

    def ast_guard(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "ast_guard")
        return guard_ast_execution_node(
            state,
            allowed_import_roots=self.request.allowed_import_roots,
            defender=self.orchestrator.defender,
            emit_analysis_event=self._emit_analysis_event,
            record_trajectory=self._trajectory,
        )

    def pytest(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "pytest")
        return run_pytest_execution_node(
            state,
            workspace=self.workspace,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            docker_image=self.orchestrator.sandbox_docker_image,
            docker_memory=self.orchestrator.sandbox_memory,
            docker_cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
            defender=self.orchestrator.defender,
            emit_analysis_event=self._emit_analysis_event,
            record_trajectory=self._trajectory,
        )

    def adversary(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "adversary")
        return run_adversary_execution_node(
            state,
            issue_text=self.request.issue_text,
            workspace=self.workspace,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            docker_image=self.orchestrator.sandbox_docker_image,
            docker_memory=self.orchestrator.sandbox_memory,
            docker_cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
            adversary=self.orchestrator.adversary,
            emit_analysis_event=self._emit_analysis_event,
            record_trajectory=self._trajectory,
        )

    def mutation(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "mutation")
        return run_mutation_execution_node(
            state,
            workspace=self.workspace,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            docker_image=self.orchestrator.sandbox_docker_image,
            docker_memory=self.orchestrator.sandbox_memory,
            docker_cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
            adversary=self.orchestrator.adversary,
            record_trajectory=self._trajectory,
        )

    def judge(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "judge")
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
            return self._transition_to(state, "finalize")
        survivors = "; ".join(state["mutation"].survival_details) or "none"
        state["previous_error"] = (
            f"Judge verdict: {verdict}. Mutation kill rate: {state['mutation'].kill_rate:.3f}. "
            f"Surviving mutants: {survivors}"
        )
        return self._retry_or_finalize(state)

    def finalize(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "finalize")
        graph_trace = [*state.get("node_trace", []), "FINALIZE"]
        outcome = persist_run_outcome(
            self.orchestrator.store,
            state,
            run_id=self.run_id,
            issue_id=self.request.issue_id,
            repo_name=self.repo_name,
            memory_key=self.memory_key,
            evidence_level=self.request.evidence_level,
            report_persona=self.request.persona,
        )
        record = outcome.record
        approved = outcome.approved
        error = outcome.error
        attempts = outcome.attempts
        llm_result = state.get("llm_result")
        if not approved:
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
        reward = state.get("reward")
        mutation = state.get("mutation")
        self._emit_analysis_event(
            AnalysisEventType.FINAL_VERDICT_PASSED if approved else AnalysisEventType.FINAL_VERDICT_FAILED,
            state,
            phase="finalize",
            error_category=(
                None
                if approved
                else str(state.get("control_status") or "analysis_failed").casefold()
            ),
            recoverable=False,
            safe_metrics={
                "pre_loc": record.pre_loc,
                "post_loc": record.post_loc,
                "pre_cc": record.pre_cc,
                "post_cc": record.post_cc,
                "self_heal_count": record.self_heal_count,
                "reward": getattr(reward, "reward", None),
                "mutation_kill_rate": getattr(mutation, "kill_rate", None),
            },
        )
        return self._transition_to(state, "finalize")

    def _write_artifacts(self, state: ExecutionState, report: str) -> None:
        write_run_artifacts(self.orchestrator.run_root, self.run_id, state, report)

    def _retry_or_finalize(self, state: ExecutionState) -> ExecutionState:
        return retry_or_finalize(state)

    def _close_round(self, state: ExecutionState, **updates) -> None:
        close_debate_round(state, **updates)

    @staticmethod
    def _transition_to(state: ExecutionState, target: WorkflowNode) -> ExecutionState:
        return transition_to(state, target)

    def _trajectory(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
        metadata: dict | None = None,
        reward: RewardBreakdown | None = None,
    ) -> None:
        self.observability.record_trajectory(
            attempt=int(state.get("attempt", 0)),
            status=status,
            message=message,
            agent=agent,
            metadata=metadata,
            reward=reward,
        )

    def _emit_analysis_event(
        self,
        event_type: AnalysisEventType,
        state: ExecutionState,
        *,
        phase: str | None = None,
        error_category: str | None = None,
        recoverable: bool | None = None,
        safe_metrics: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        self.observability.emit_analysis_event(
            event_type,
            attempt=int(state.get("attempt", 0)),
            phase=phase,
            error_category=error_category,
            recoverable=recoverable,
            safe_metrics=safe_metrics,
        )

    def _phase_started(self, state: ExecutionState, phase: str) -> None:
        self._emit_analysis_event(
            AnalysisEventType.PHASE_STARTED,
            state,
            phase=phase,
        )

    @staticmethod
    def _rewrite_metadata(rewrite: AstRewriteResult) -> dict[str, object]:
        return rewrite_metadata(rewrite)


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"


def _request_with_memory(request: RefactorRequest, memory_context: str | None) -> RefactorRequest:
    return prepare_request_with_memory(request, memory_context)


def _summarize_failure(result: SandboxResult) -> str:
    return summarize_pytest_failure(result)


def _summarize_adversarial_failure(result: AdversarialTestResult) -> str:
    return summarize_adversarial_failure(result)


def _summarize_adversary_pass(result: AdversarialTestResult) -> str:
    return summarize_adversary_pass(result)


def _summarize_critique(critique: AdversarialCritique) -> str:
    return summarize_critique(critique)


def _summarize_mutation(result: MutationTestResult) -> str:
    return summarize_mutation(result)


def _summarize_judge(reward: RewardBreakdown) -> str:
    return (
        "裁判评分="
        f"{reward.reward:.2f}；LOC 改善={reward.delta_loc}；圈复杂度改善={reward.delta_cc}；"
        f"变异击杀率={reward.mutation_kill_rate:.2f}；重试次数={reward.retry_count}。"
    )


def _code_change_percent(before: str, after: str) -> float:
    return code_change_percent(before, after)


def _combined_mutation_tests_path(
    workspace: Path,
    baseline_tests: Path,
    adversarial_test_file: Path | None,
) -> Path:
    return combined_mutation_tests_path(
        workspace,
        baseline_tests,
        adversarial_test_file,
    )

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
