from __future__ import annotations

from pathlib import Path

from refactor_agent.debate_state import render_mermaid_state_diagram
from refactor_agent.models import (
    AdversarialTestResult,
    AstRewriteResult,
    CandidateValidationResult,
    DebateRound,
    EvidenceLevel,
    LLMUsage,
    MutationTestResult,
    PerformanceProfile,
    ReportPersona,
    RewardBreakdown,
    RunRecord,
    SandboxResult,
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


def build_report(
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
    technical = build_technical_report(
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


def build_technical_report(
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

