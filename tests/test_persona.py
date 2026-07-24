from pathlib import Path

from refactor_agent.models import (
    AdversarialTestResult,
    DebateRound,
    EvidenceLevel,
    RefactorRunResult,
    ReportPersona,
    RunRecord,
)
from refactor_agent.persona import build_persona_report, extract_persona_markdown, render_persona_markdown


def test_persona_changes_wording_without_changing_evidence_or_metrics(tmp_path: Path):
    result = RefactorRunResult(
        record=RunRecord(
            run_id="run-1",
            repo_name="local/snippet",
            pre_loc=10,
            post_loc=2,
            pre_cc=4,
            post_cc=1,
            self_heal_count=0,
            status="SUCCESS",
            evidence_level=EvidenceLevel.GENERATED_TESTS,
            report_persona=ReportPersona.TSUNDERE,
        ),
        report_markdown="",
        workspace_path=tmp_path,
        attempts=1,
        evidence_level=EvidenceLevel.GENERATED_TESTS,
        report_persona=ReportPersona.TSUNDERE,
        debate_rounds=[DebateRound(round=1, pytest_passed=True, converged=True)],
    )
    strict = build_persona_report(result, ReportPersona.STRICT)
    tsundere = build_persona_report(result, ReportPersona.TSUNDERE)
    assert strict.opening_verdict != tsundere.opening_verdict
    assert strict.metrics_assessment == tsundere.metrics_assessment
    assert strict.evidence_warning == tsundere.evidence_warning
    markdown = render_persona_markdown(tsundere)
    assert "多 Agent 对抗摘要" in markdown
    assert "GENERATED_TESTS" not in markdown
    assert len(markdown) >= 100
    assert extract_persona_markdown("# report\n\n" + markdown).startswith("#### 人格化代码审判")
    assert "不能等同用户或仓库回归测试" in markdown


def test_tsundere_report_is_stable_and_mentions_failure_fact(tmp_path: Path):
    result = RefactorRunResult(
        record=RunRecord(
            run_id="stable-run",
            repo_name="local/snippet",
            pre_loc=12,
            post_loc=14,
            pre_cc=3,
            post_cc=5,
            self_heal_count=1,
            status="FAILED",
            evidence_level=EvidenceLevel.STATIC,
        ),
        report_markdown="",
        workspace_path=tmp_path,
        attempts=2,
        evidence_level=EvidenceLevel.STATIC,
        adversarial_result=AdversarialTestResult(
            generated=2,
            passed=False,
            returncode=1,
        ),
    )
    first = render_persona_markdown(build_persona_report(result, ReportPersona.TSUNDERE))
    second = render_persona_markdown(build_persona_report(result, ReportPersona.TSUNDERE))
    assert first == second
    assert "对抗测试已经把漏洞戳出来了" in first
    assert "作为 AI" not in first
    assert "综上所述" not in first
