from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from refactor_agent.config import AppSettings
from refactor_agent.models import GitHubAutomationResult, RepositoryJobKind
from refactor_agent.snippet_submission import execute_snippet_submission


def test_snippet_submission_builds_review_job_and_reads_report(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    timestamps = iter(
        [
            datetime(2026, 8, 2, 1, 2, 3, 456789, tzinfo=timezone.utc),
            datetime(2026, 8, 2, 1, 2, 4, tzinfo=timezone.utc),
        ]
    )

    class Processor:
        def __init__(self, settings: AppSettings) -> None:
            captured["settings"] = settings

        def process(self, job):
            captured["job"] = job
            report = tmp_path / "runs" / "run-1" / "artifacts" / "report.md"
            report.parent.mkdir(parents=True)
            report.write_text("snippet report", encoding="utf-8")
            return GitHubAutomationResult(
                job_id=job.job_id,
                repo_full_name="local/snippet",
                issue_number=None,
                run_id="run-1",
                status="DRY_RUN",
            )

    execution = execute_snippet_submission(
        source_text="def add(a, b):\n    return a + b\n",
        request_text="  review this  ",
        tests_text=None,
        mode="review",
        persona="tsundere",
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        sandbox_backend="docker",
        mock=True,
        settings_factory=lambda: AppSettings(max_retry=5),
        processor_factory=Processor,
        utc_now=lambda: next(timestamps),
    )

    settings = captured["settings"]
    job = captured["job"]
    assert settings.run_root == tmp_path / "runs"
    assert settings.database_path == tmp_path / "runs.sqlite"
    assert settings.sandbox_backend == "docker"
    assert settings.mock_llm is True
    assert settings.max_retry == 5
    assert job.job_kind == RepositoryJobKind.SNIPPET
    assert job.job_id == "snippet-cli-20260802010203456789"
    assert job.delivery_id == "snippet-cli:1785632524.0"
    assert job.issue_text == "review this"
    assert job.snippet_mode == "REVIEW"
    assert job.persona == "TSUNDERE"
    assert execution.report_markdown == "snippet report"
    assert execution.exit_code == 0


def test_snippet_submission_maps_verified_mode_and_failed_result(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Processor:
        def __init__(self, settings: AppSettings) -> None:
            pass

        def process(self, job):
            captured["job"] = job
            report = tmp_path / "runs" / "run-2" / "artifacts" / "report.md"
            report.parent.mkdir(parents=True)
            report.write_text("failed report", encoding="utf-8")
            return GitHubAutomationResult(
                job_id=job.job_id,
                repo_full_name="local/snippet",
                issue_number=None,
                run_id="run-2",
                status="FAILED",
            )

    execution = execute_snippet_submission(
        source_text="def add(a, b): return a + b",
        request_text="review",
        tests_text="def test_add(): pass",
        mode="verified-refactor",
        persona="strict",
        run_root=tmp_path / "runs",
        database_path=None,
        sandbox_backend="subprocess",
        mock=False,
        settings_factory=AppSettings,
        processor_factory=Processor,
    )

    assert captured["job"].snippet_mode == "VERIFIED_REFACTOR"
    assert captured["job"].snippet_tests == "def test_add(): pass"
    assert captured["job"].persona == "STRICT"
    assert execution.report_markdown == "failed report"
    assert execution.exit_code == 1


def test_snippet_submission_handles_result_without_run_id(tmp_path: Path) -> None:
    class Processor:
        def __init__(self, settings: AppSettings) -> None:
            pass

        def process(self, job):
            return GitHubAutomationResult(
                job_id=job.job_id,
                repo_full_name="local/snippet",
                issue_number=None,
                status="FAILED",
            )

    execution = execute_snippet_submission(
        source_text="pass",
        request_text="review",
        tests_text=None,
        mode="review",
        persona="strict",
        run_root=tmp_path / "runs",
        database_path=None,
        sandbox_backend="subprocess",
        mock=True,
        settings_factory=AppSettings,
        processor_factory=Processor,
    )

    assert execution.report_markdown is None
    assert execution.exit_code == 1


@pytest.mark.parametrize(
    ("mode", "persona", "tests_text", "message"),
    [
        ("unknown", "strict", None, "mode must be review or verified-refactor"),
        ("review", "unknown", None, "persona must be strict or tsundere"),
        ("verified-refactor", "strict", None, "verified-refactor mode requires tests"),
    ],
)
def test_snippet_submission_rejects_invalid_normalized_input(
    tmp_path: Path,
    mode: str,
    persona: str,
    tests_text: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        execute_snippet_submission(
            source_text="pass",
            request_text="review",
            tests_text=tests_text,
            mode=mode,
            persona=persona,
            run_root=tmp_path / "runs",
            database_path=None,
            sandbox_backend="subprocess",
            mock=True,
        )
