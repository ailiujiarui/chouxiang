from __future__ import annotations

from refactor_agent.models import EvidenceLevel, PersonaReport, RefactorRunResult, ReportPersona


def build_persona_report(
    result: RefactorRunResult,
    persona: ReportPersona,
) -> PersonaReport:
    approved = result.record.status == "SUCCESS"
    evidence = result.evidence_level
    if persona == ReportPersona.TSUNDERE:
        opening = "哼，把代码交出来吧。本小姐只是看不惯这些分支继续丢人，可不是特意帮你。"
        final = (
            "这次勉强过关。别误会，我认可的是证据，不是你那堆原始结构。"
            if approved
            else "连裁判席都没走下来，还想听夸奖？先按失败证据把代码修明白。"
        )
    else:
        opening = "代码审判已完成，以下结论严格以执行证据为准。"
        final = "候选通过当前证据链。" if approved else "候选未通过当前证据链，不应采用。"
    evidence_warning = {
        EvidenceLevel.STATIC: "只有静态分析和导入 smoke，候选未经行为验证。",
        EvidenceLevel.GENERATED_TESTS: "只通过系统推导测试，不能等同用户或仓库回归测试。",
        EvidenceLevel.USER_TESTS: "通过用户 pytest 与自动攻击测试。",
        EvidenceLevel.REPOSITORY_TESTS: "通过仓库测试与自动攻击测试。",
    }[evidence]
    debate = []
    for round_item in result.debate_rounds:
        agents = "、".join(message.agent for message in round_item.messages)
        debate.append(
            f"第 {round_item.round} 轮：{agents or '无记录'}；"
            f"pytest={'通过' if round_item.pytest_passed else '失败'}；"
            f"收敛={'是' if round_item.converged else '否'}。"
        )
    rewrite = result.ast_rewrite
    changed = "、".join(rewrite.changed_regions) if rewrite and rewrite.changed_regions else "无"
    record = result.record
    return PersonaReport(
        persona=persona,
        opening_verdict=opening,
        ast_assessment=f"AST 受控改写区域：{changed}。",
        debate_summary=debate,
        metrics_assessment=(
            f"LOC {record.pre_loc} -> {record.post_loc}；"
            f"CC {record.pre_cc} -> {record.post_cc}。"
        ),
        evidence_warning=evidence_warning,
        final_verdict=final,
    )


def render_persona_markdown(report: PersonaReport) -> str:
    debate = "\n".join(f"- {item}" for item in report.debate_summary) or "- 没有形成有效对抗轮次。"
    return (
        "\n#### 人格化代码审判\n\n"
        f"{report.opening_verdict}\n\n"
        f"- {report.ast_assessment}\n"
        f"- {report.metrics_assessment}\n"
        f"- 证据边界：{report.evidence_warning}\n\n"
        "##### 多 Agent 对抗摘要\n\n"
        f"{debate}\n\n"
        f"**最终判词：{report.final_verdict}**\n"
    )
