from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from refactor_agent.config import AppSettings
from refactor_agent.models import (
    GitHubAutomationResult,
    GitHubRefactorJob,
    RepositoryJobKind,
)
from refactor_agent.snippet import SnippetRefactorService


class SnippetProcessor(Protocol):
    def process(self, job: GitHubRefactorJob) -> GitHubAutomationResult: ...


SettingsFactory = Callable[[], AppSettings]
SnippetProcessorFactory = Callable[[AppSettings], SnippetProcessor]
UtcNow = Callable[[], datetime]


@dataclass(frozen=True)
class SnippetSubmissionResult:
    automation_result: GitHubAutomationResult
    report_markdown: str | None
    exit_code: int


def execute_snippet_submission(
    *,
    source_text: str,
    request_text: str,
    tests_text: str | None,
    mode: str,
    persona: str,
    run_root: Path,
    database_path: Path | None,
    sandbox_backend: str,
    mock: bool,
    settings_factory: SettingsFactory = AppSettings.from_env,
    processor_factory: SnippetProcessorFactory = SnippetRefactorService,
    utc_now: UtcNow = lambda: datetime.now(timezone.utc),
) -> SnippetSubmissionResult:
    """Build and execute one snippet job without depending on CLI input/output."""

    if mode not in {"review", "verified-refactor"}:
        raise ValueError("mode must be review or verified-refactor")
    if persona not in {"strict", "tsundere"}:
        raise ValueError("persona must be strict or tsundere")
    if mode == "verified-refactor" and not tests_text:
        raise ValueError("verified-refactor mode requires tests")

    settings = settings_factory().model_copy(
        update={
            "run_root": run_root,
            "database_path": database_path,
            "sandbox_backend": sandbox_backend,
            "mock_llm": mock,
        }
    )
    job_time = utc_now()
    delivery_time = utc_now()
    job = GitHubRefactorJob(
        job_kind=RepositoryJobKind.SNIPPET,
        job_id=f"snippet-cli-{job_time.strftime('%Y%m%d%H%M%S%f')}",
        delivery_id=f"snippet-cli:{delivery_time.timestamp()}",
        repo_full_name="local/snippet",
        default_branch=None,
        issue_number=None,
        issue_title="CLI snippet code review",
        issue_text=request_text.strip(),
        target_path="snippet.py",
        tests_path="test_snippet.py",
        event_name="snippet_cli",
        action="submitted",
        snippet_source=source_text,
        snippet_tests=tests_text,
        snippet_mode="REVIEW" if mode == "review" else "VERIFIED_REFACTOR",
        persona="STRICT" if persona == "strict" else "TSUNDERE",
    )
    automation_result = processor_factory(settings).process(job)
    if not automation_result.run_id:
        return SnippetSubmissionResult(
            automation_result=automation_result,
            report_markdown=None,
            exit_code=1,
        )
    report_path = (
        settings.run_root
        / automation_result.run_id
        / "artifacts"
        / "report.md"
    )
    return SnippetSubmissionResult(
        automation_result=automation_result,
        report_markdown=report_path.read_text(encoding="utf-8"),
        exit_code=0 if automation_result.status == "DRY_RUN" else 1,
    )
