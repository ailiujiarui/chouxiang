from pathlib import Path

from refactor_agent.models import EvidenceLevel, ReportPersona, RunRecord
from refactor_agent.orchestrator_report import build_report, build_technical_report


def test_report_renderer_output(tmp_path: Path):
    record = _record()

    rendered = build_report(record, tmp_path, None, None, None)

    assert rendered.startswith("# Code Judge Report")
    assert "Decision: ADOPT WITH EVIDENCE" in rendered
    assert "Technical appendix" in rendered


def test_technical_report_renderer_output(tmp_path: Path):
    record = _record()

    rendered = build_technical_report(
        record,
        tmp_path,
        None,
        None,
        None,
        evidence_level=EvidenceLevel.REPOSITORY_TESTS,
        report_persona=ReportPersona.STRICT,
    )

    assert "Refactor Agent Report" in rendered
    assert "Evidence Level" in rendered


def _record() -> RunRecord:
    return RunRecord(
        run_id="report-run",
        repo_name="octo/demo",
        pre_loc=10,
        post_loc=8,
        pre_cc=3,
        post_cc=2,
        self_heal_count=0,
        status="SUCCESS",
        evidence_level=EvidenceLevel.REPOSITORY_TESTS,
        report_persona=ReportPersona.STRICT,
    )
