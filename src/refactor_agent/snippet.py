from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from refactor_agent.adversary import generate_adversarial_tests
from refactor_agent.config import AppSettings
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.llm import DeepSeekClient, MockRefactorClient, RefactorClient
from refactor_agent.models import (
    GitHubAutomationResult,
    GitHubRefactorJob,
    EvidenceLevel,
    RefactorRequest,
    RefactorRunResult,
    ReportPersona,
    RepositoryJobKind,
)
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.persona import build_persona_report, render_persona_markdown
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
            return self._review(job, control)
        if not job.snippet_tests:
            raise ValueError("Verified refactor mode requires pytest source.")
        return self._verified_refactor(job, control)

    def _review(
        self,
        job: GitHubRefactorJob,
        control: ExecutionControl,
    ) -> GitHubAutomationResult:
        llm_client = self.llm_client or self._default_llm_client()
        generated = generate_adversarial_tests(
            job.snippet_source or "",
            "snippet",
            job.issue_text,
        )
        if not generated and isinstance(llm_client, DeepSeekClient):
            generated = llm_client.generate_tests(
                job.snippet_source or "",
                job.issue_text,
                "snippet",
            )
        evidence = EvidenceLevel.GENERATED_TESTS if generated else EvidenceLevel.STATIC
        tests = generated or (
            "import snippet\n\n"
            "def test_generated_module_imports():\n"
            "    assert snippet is not None\n"
        )
        return self._run_refactor(job, tests, evidence, control, llm_client)

    def _verified_refactor(
        self,
        job: GitHubRefactorJob,
        control: ExecutionControl,
    ) -> GitHubAutomationResult:
        return self._run_refactor(
            job,
            job.snippet_tests or "",
            EvidenceLevel.USER_TESTS,
            control,
            self.llm_client or self._default_llm_client(),
        )

    def _run_refactor(
        self,
        job: GitHubRefactorJob,
        tests_source: str,
        evidence_level: EvidenceLevel,
        control: ExecutionControl,
        llm_client: RefactorClient,
    ) -> GitHubAutomationResult:
        with tempfile.TemporaryDirectory(prefix="refactor-agent-snippet-") as directory:
            root = Path(directory)
            target = root / "snippet.py"
            tests = root / "test_snippet.py"
            target.write_text(job.snippet_source or "", encoding="utf-8")
            tests.write_text(tests_source, encoding="utf-8")
            orchestrator = RefactorOrchestrator(
                llm_client=llm_client,
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
                    evidence_level=evidence_level,
                    persona=ReportPersona(job.persona),
                ),
                execution_control=control,
            )
            _append_persona_commentary(
                self.settings.run_root / result.record.run_id / "artifacts" / "report.md",
                result,
                ReportPersona(job.persona),
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


def _append_persona_commentary(
    report_path: Path,
    result: RefactorRunResult,
    persona: ReportPersona,
) -> None:
    commentary = render_persona_markdown(build_persona_report(result, persona))
    with report_path.open("a", encoding="utf-8", newline="") as report:
        report.write(commentary)
