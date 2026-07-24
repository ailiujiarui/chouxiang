from __future__ import annotations

import hashlib
import re
from pathlib import Path

from refactor_agent.models import EvidenceLevel, PersonaCopy, PersonaReport, RefactorRunResult, ReportPersona


_AI_FRAMING = (
    "作为AI",
    "作为一个AI",
    "综上所述",
    "基于当前证据",
    "本次审查",
    "建议您",
)
_FORBIDDEN_PERSONA_MARKS = ("脏话", "废物", "蠢货")


def build_persona_report(
    result: RefactorRunResult,
    persona: ReportPersona,
    persona_copy: PersonaCopy | None = None,
) -> PersonaReport:
    approved = result.record.status == "SUCCESS"
    evidence = result.evidence_level
    rewrite = result.ast_rewrite
    changed = "、".join(rewrite.changed_regions) if rewrite and rewrite.changed_regions else "目标区域"
    record = result.record
    metrics = f"LOC {record.pre_loc} -> {record.post_loc}，CC {record.pre_cc} -> {record.post_cc}"
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
            f"第 {round_item.round} 轮：{agents or '无记录'}，"
            f"pytest={'通过' if round_item.pytest_passed else '失败'}，"
            f"收敛={'是' if round_item.converged else '否'}。"
        )

    if persona == ReportPersona.TSUNDERE:
        opening, commentary, final = _tsundere_copy(result, changed, metrics, approved)
        if persona_copy is not None:
            opening = persona_copy.opening_verdict
            commentary = persona_copy.commentary
            final = persona_copy.closing_verdict
    else:
        opening = "代码审判已完成，以下结论严格以执行证据为准。"
        commentary = (
            "本次审判仅对代码结构和已执行证据作出评估，不对未测试的生产行为做额外承诺。"
            "只有在行为、安全和可维护性都有证据时，才建议合并。"
        )
        final = "候选通过当前证据链。" if approved else "候选未通过当前证据链，不应采用。"

    return PersonaReport(
        persona=persona,
        opening_verdict=opening,
        commentary=commentary,
        ast_assessment=f"AST 受控改写区域：{changed}。",
        debate_summary=debate,
        metrics_assessment=metrics,
        evidence_warning=evidence_warning,
        final_verdict=final,
    )


def _tsundere_copy(
    result: RefactorRunResult,
    changed: str,
    metrics: str,
    approved: bool,
) -> tuple[str, str, str]:
    record = result.record
    seed = int(hashlib.sha256(record.run_id.encode()).hexdigest()[:8], 16)
    openings = (
        "哼，终于把这段代码端上来了。先别得意，我只是在检查它有没有继续添乱。",
        "把代码放这儿。本小姐看的是它做了什么，不是它装得有多像重构。",
        "行，轮到审判了。别把沉默当夸奖，我还没说它值得留下。",
    )
    opening = openings[seed % len(openings)]

    if approved:
        detail = f"这次动到的是 {changed}，{metrics}。至少改动没有越界，算你把分寸找回来了。"
        if record.self_heal_count:
            detail += f"中间返工了 {record.self_heal_count} 轮，慢是慢了点，最后没再把问题藏起来。"
        final = "勉强通过。别误会，我认可的是这条证据链，不是你原来那套绕路写法。"
    else:
        detail = f"{changed} 这里仍然没有站稳，{metrics}。失败就是失败，别拿漂亮的 diff 糊弄裁判。"
        if result.adversarial_result and not result.adversarial_result.passed:
            detail += "对抗测试已经把漏洞戳出来了，连反例都接不住，还想装作完成？"
        elif result.mutation_result and result.mutation_result.survived:
            detail += "还有变异存活，说明测试只是在鼓掌，没有真正拦住错误。"
        else:
            detail += "先把失败证据处理干净，再来讨夸奖。"
        final = "不通过。修好失败路径，补足证据，再来浪费我的时间。"
    return opening, detail, final


def render_persona_markdown(report: PersonaReport) -> str:
    review_body = report.commentary or _substantive_review(report)
    debate = "\n".join(f"- {item}" for item in report.debate_summary) or "- 没有形成有效对抗轮次。"
    rendered = (
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
    _assert_persona_safety(rendered, report.persona)
    return rendered


def _substantive_review(report: PersonaReport) -> str:
    if report.persona == ReportPersona.TSUNDERE:
        return "哼，代码我看过了。具体结论写在下面，别把这句话当成免检通行证。"
    return (
        "本次审判仅对代码结构和已执行证据作出评估，不对未测试的生产行为做额外承诺。"
        "如果证据等级不是用户或仓库测试，建议补齐回归测试并复核 diff。"
    )


def _assert_persona_safety(text: str, persona: ReportPersona) -> None:
    if persona != ReportPersona.TSUNDERE:
        return
    lowered = text.casefold()
    if any(phrase.casefold() in lowered for phrase in _AI_FRAMING + _FORBIDDEN_PERSONA_MARKS):
        raise ValueError("persona report contains forbidden framing or abuse")
    if re.search(r"[\U0001F300-\U0001FAFF]", text):
        raise ValueError("persona report must not contain emoji")


def extract_persona_markdown(markdown: str) -> str:
    """Return only the persona section for compact UI surfaces."""
    start = markdown.find("#### 人格化代码审判")
    if start < 0:
        return "人格报告暂不可用。"
    end = markdown.find("\n<details>", start)
    return markdown[start:] if end < 0 else markdown[start:end].rstrip()


def inject_persona_report(
    report_path: Path,
    result: RefactorRunResult,
    persona: ReportPersona,
    persona_client: object | None = None,
) -> None:
    """Place persona commentary before the technical appendix when present."""
    existing = report_path.read_text(encoding="utf-8")
    persona_copy = generate_persona_copy(persona_client, result) if persona_client and persona == ReportPersona.TSUNDERE else None
    commentary = render_persona_markdown(build_persona_report(result, persona, persona_copy))
    marker = "\n<details>"
    updated = existing.replace(marker, f"{commentary}{marker}", 1) if marker in existing else existing + commentary
    report_path.write_text(updated, encoding="utf-8", newline="")


def generate_persona_copy(client: object | None, result: RefactorRunResult) -> PersonaCopy | None:
    if client is None or not hasattr(client, "generate_persona_copy"):
        return None
    from refactor_agent.llm import LLMError

    try:
        generated = client.generate_persona_copy(_persona_facts(result))
        _assert_persona_safety(
            "\n".join(
                [generated.opening_verdict, generated.commentary, generated.closing_verdict]
            ),
            ReportPersona.TSUNDERE,
        )
        return generated
    except (LLMError, ValueError, TypeError, AttributeError):
        return None


def _persona_facts(result: RefactorRunResult) -> str:
    record = result.record
    rewrite = result.ast_rewrite
    changed = ", ".join(rewrite.changed_regions) if rewrite and rewrite.changed_regions else "none"
    adversarial = result.adversarial_result
    mutation = result.mutation_result
    return "\n".join(
        [
            "Write a persona report from these facts. Facts are authoritative.",
            f"Status: {record.status}",
            f"Evidence level: {result.evidence_level.value}",
            f"Changed regions: {changed}",
            f"LOC: {record.pre_loc} -> {record.post_loc}",
            f"Cyclomatic complexity: {record.pre_cc} -> {record.post_cc}",
            f"Retry count: {record.self_heal_count}",
            f"Pytest passed: {result.last_sandbox_result.passed if result.last_sandbox_result else 'unknown'}",
            f"Adversarial passed: {adversarial.passed if adversarial else 'unknown'}",
            f"Mutation survived: {mutation.survived if mutation else 'unknown'}",
            f"Code-specific review: {result.report_markdown[:1200]}",
        ]
    )
