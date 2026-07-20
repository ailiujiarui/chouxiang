from __future__ import annotations

from pathlib import Path

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
    review_body = _substantive_review(report)
    debate = "\n".join(f"- {item}" for item in report.debate_summary) or "- 没有形成有效对抗轮次。"
    return (
        "\n#### 人格化代码审判\n\n"
        f"{report.opening_verdict}\n\n"
        f"{review_body}\n\n"
        f"- {report.ast_assessment}\n"
        f"- {report.metrics_assessment}\n"
        f"- 证据边界：{report.evidence_warning}\n\n"
        "##### 多 Agent 对抗摘要\n\n"
        f"{debate}\n\n"
        f"**最终判词：{report.final_verdict}**\n"
    )


def _substantive_review(report: PersonaReport) -> str:
    if report.persona == ReportPersona.TSUNDERE:
        return (
            "\u54fc\uff0c\u8fd9\u6b21\u6211\u628a\u4ee3\u7801\u7684\u5206\u652f\u3001\u6539\u5199\u533a\u57df\u548c\u9a8c\u8bc1\u8fb9\u754c\u90fd\u91cd\u65b0\u770b\u4e86\u4e00\u904d\u3002"
            "\u4f60\u53ef\u4ee5\u5f97\u610f\uff0c\u4f46\u522b\u6025\u7740\u628a\u5b83\u5f53\u6210\u7ec8\u7a3f\uff1b\u6211\u8ba4\u53ef\u7684\u662f\u5f53\u524d\u8bc1\u636e\uff0c\u4e0d\u662f\u4f60\u7684\u8fd0\u6c14\u3002"
            "\u5982\u679c\u6d4b\u8bd5\u53ea\u662f\u7cfb\u7edf\u63a8\u5bfc\u51fa\u6765\u7684\uff0c\u90a3\u5c31\u8865\u4e0a\u771f\u6b63\u7684\u56de\u5f52\u6d4b\u8bd5\uff0c\u518d\u6765\u8c08\u91c7\u7528\u3002"
        )
    return (
        "\u672c\u6b21\u5ba1\u5224\u4ec5\u5bf9\u4ee3\u7801\u7ed3\u6784\u548c\u5df2\u6267\u884c\u8bc1\u636e\u4f5c\u51fa\u8bc4\u4f30\uff0c\u4e0d\u5bf9\u672a\u6d4b\u8bd5\u7684\u751f\u4ea7\u884c\u4e3a\u505a\u989d\u5916\u627f\u8bfa\u3002"
        "\u5982\u679c\u8bc1\u636e\u7b49\u7ea7\u4e0d\u662f\u7528\u6237\u6216\u4ed3\u5e93\u6d4b\u8bd5\uff0c\u5efa\u8bae\u8865\u9f50\u56de\u5f52\u6d4b\u8bd5\u5e76\u590d\u6838 diff\uff1b"
        "\u53ea\u6709\u5728\u884c\u4e3a\u3001\u5b89\u5168\u548c\u53ef\u7ef4\u62a4\u6027\u90fd\u6709\u8bc1\u636e\u65f6\uff0c\u624d\u5efa\u8bae\u5408\u5e76\u3002"
    )


def extract_persona_markdown(markdown: str) -> str:
    """Return only the persona section for compact UI surfaces."""
    start = markdown.find("#### \u4eba\u683c\u5316\u4ee3\u7801\u5ba1\u5224")
    if start < 0:
        return "\u4eba\u683c\u62a5\u544a\u6682\u4e0d\u53ef\u7528\u3002"
    end = markdown.find("\n<details>", start)
    return markdown[start:] if end < 0 else markdown[start:end].rstrip()


def inject_persona_report(report_path: Path, result: RefactorRunResult, persona: ReportPersona) -> None:
    """Place persona commentary before the technical appendix when present."""
    existing = report_path.read_text(encoding="utf-8")
    commentary = render_persona_markdown(build_persona_report(result, persona))
    marker = "\n<details>"
    if marker in existing:
        updated = existing.replace(marker, f"{commentary}{marker}", 1)
    else:
        updated = existing + commentary
    report_path.write_text(updated, encoding="utf-8", newline="")
