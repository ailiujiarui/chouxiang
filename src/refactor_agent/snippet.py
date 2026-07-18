from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.artifacts import RunArtifactWriter
from refactor_agent.ast_analyzer import analyze_ast
from refactor_agent.config import AppSettings
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.llm import DeepSeekClient, MockRefactorClient, RefactorClient
from refactor_agent.models import (
    GitHubAutomationResult,
    GitHubRefactorJob,
    RefactorRequest,
    RepositoryJobKind,
    RunRecord,
)
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


class SnippetRefactorService:
    def __init__(
        self,
        settings: AppSettings,
        llm_client: RefactorClient | None = None,
    ) -> None:
        self.settings = settings
        self.llm_client = llm_client

    def process(
        self,
        job: GitHubRefactorJob,
        execution_control: ExecutionControl | None = None,
    ) -> GitHubAutomationResult:
        if job.job_kind != RepositoryJobKind.SNIPPET:
            raise ValueError("Snippet service only accepts SNIPPET jobs.")
        if job.snippet_source is None or job.snippet_mode is None:
            raise ValueError("Snippet job payload is incomplete.")
        control = execution_control or ExecutionControl(
            deadline_at=datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.job_deadline_seconds)
        )
        control.checkpoint("before-snippet-processing")
        if job.snippet_mode == "REVIEW":
            return self._review(job)
        if not job.snippet_tests:
            raise ValueError("Verified refactor mode requires pytest source.")
        return self._verified_refactor(job, control)

    def _review(self, job: GitHubRefactorJob) -> GitHubAutomationResult:
        analysis = analyze_ast(job.snippet_source or "")
        run_id = f"snippet-review-{uuid4().hex}"
        run_dir = self.settings.run_root / run_id
        report = _render_review(job, analysis.loc, analysis.cyclomatic_complexity, analysis)
        writer = RunArtifactWriter(run_dir)
        writer.write_sources(job.snippet_source or "", job.snippet_source or "")
        writer.write_log("pytest.log", "Not executed: REVIEW mode.\n")
        writer.write_log("adversary.log", "Not executed: REVIEW mode.\n")
        writer.write_json("mutation.json", {"status": "NOT_EXECUTED", "reason": "REVIEW mode"})
        writer.write_report(report)
        SQLiteRunStore(self.settings.resolved_database_path).save(
            RunRecord(
                run_id=run_id,
                issue_id=job.job_id,
                repo_name="local/snippet",
                pre_loc=analysis.loc,
                post_loc=None,
                pre_cc=analysis.cyclomatic_complexity,
                post_cc=None,
                self_heal_count=0,
                status="REVIEWED",
                error=None,
            )
        )
        return GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name="local/snippet",
            issue_number=None,
            run_id=run_id,
            status="DRY_RUN",
        )

    def _verified_refactor(
        self,
        job: GitHubRefactorJob,
        control: ExecutionControl,
    ) -> GitHubAutomationResult:
        with tempfile.TemporaryDirectory(prefix="refactor-agent-snippet-") as directory:
            root = Path(directory)
            target = root / "snippet.py"
            tests = root / "test_snippet.py"
            target.write_text(job.snippet_source or "", encoding="utf-8")
            tests.write_text(job.snippet_tests or "", encoding="utf-8")
            orchestrator = RefactorOrchestrator(
                llm_client=self.llm_client or self._default_llm_client(),
                run_root=self.settings.run_root,
                store=SQLiteRunStore(self.settings.resolved_database_path),
                pytest_timeout_seconds=self.settings.pytest_timeout_seconds,
                sandbox_backend=self.settings.sandbox_backend,
                sandbox_docker_image=self.settings.sandbox_docker_image,
                sandbox_memory=self.settings.sandbox_memory,
                sandbox_cpus=self.settings.sandbox_cpus,
                graph_backend=self.settings.graph_backend,
            )
            result = orchestrator.run(
                RefactorRequest(
                    target_file=target,
                    issue_text=_persona_request(job.issue_text, job.persona),
                    tests_path=tests,
                    repo_name="local/snippet",
                    issue_id=job.job_id,
                    max_retry=self.settings.max_retry,
                    allowed_import_roots=self.settings.allowed_import_roots,
                ),
                execution_control=control,
            )
            _append_persona_commentary(
                self.settings.run_root / result.record.run_id / "artifacts" / "report.md",
                job.persona,
                result.record.status,
            )
        return GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name="local/snippet",
            issue_number=None,
            run_id=result.record.run_id,
            status="DRY_RUN" if result.record.status == "SUCCESS" else "FAILED",
            error=result.record.error,
        )

    def _default_llm_client(self) -> RefactorClient:
        return MockRefactorClient() if self.settings.mock_llm else DeepSeekClient()


def _persona_request(request: str, persona: str) -> str:
    if persona != "TSUNDERE":
        return request
    return (
        request
        + "\n\n报告人格使用克制的傲娇语气和轻度挑衅，只批评代码结构；"
        "禁止针对作者身份、外貌、能力进行羞辱。"
    )


def _append_persona_commentary(report_path: Path, persona: str, status: str) -> None:
    if persona == "TSUNDERE":
        commentary = (
            "哼，这次验证算你过关了，才不是因为我特别照顾这段代码。"
            if status == "SUCCESS"
            else "连验证都没过，还想让我夸你？先把证据补齐再来。"
        )
    else:
        commentary = (
            "验证链已通过，结论以以上证据为准。"
            if status == "SUCCESS"
            else "验证链未通过，不应采用本次候选。"
        )
    with report_path.open("a", encoding="utf-8", newline="") as report:
        report.write(f"\n#### 人格点评\n\n{commentary}\n")


def _render_review(job: GitHubRefactorJob, loc: int, cc: int, analysis) -> str:
    if job.persona == "TSUNDERE":
        verdict = "才、才不是特意帮你看代码。结构问题已经列好，但没有测试就别擅自宣布胜利。"
    else:
        verdict = "静态审查完成。没有执行候选代码，以下结论不代表修复或验证成功。"
    findings = analysis.safety_findings or []
    finding_lines = [f"- `{item.rule}` line {item.lineno or '-'}: {item.message}" for item in findings]
    if not finding_lines:
        finding_lines = ["- 静态安全扫描未发现已知危险结构。"]
    return "\n".join(
        [
            "# Snippet 只读审查报告",
            "",
            "> 状态：**REVIEWED（未执行、未验证）**",
            "",
            verdict,
            "",
            f"- LOC: {loc}",
            f"- Cyclomatic Complexity: {cc}",
            f"- 函数数: {len(analysis.functions)}",
            f"- 目标区域数: {len(analysis.target_regions)}",
            "",
            "## 静态发现",
            "",
            *finding_lines,
            "",
            "## 验证边界",
            "",
            "未运行 pytest、对抗测试、变异测试或候选代码，因此没有 Reward，也不能声称代码可用。",
            "",
            f"审查要求：{job.issue_text}",
            "",
        ]
    )
