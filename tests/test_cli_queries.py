from __future__ import annotations

from pathlib import Path

from refactor_agent.cli_queries import query_job_lines, query_memory_lines
from refactor_agent.models import (
    GitHubJobRecord,
    GitHubJobStatus,
    TrajectoryMemoryRecord,
)


def test_query_job_lines_preserves_filters_and_display_fields(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Store:
        def list_github_jobs(self, limit: int = 100):
            captured["limit"] = limit
            return [
                GitHubJobRecord(
                    job_id="job-1",
                    delivery_id="delivery-1",
                    repo_full_name="owner/repository",
                    issue_number=42,
                    target_path="src/module.py",
                    tests_path="tests",
                    status=GitHubJobStatus.SUCCESS,
                    run_id="run-1",
                    pr_url=None,
                    updated_at="2026-08-02T00:00:00Z",
                )
            ]

    lines = query_job_lines(
        tmp_path / "runs.sqlite",
        7,
        store_factory=lambda path: Store(),
    )

    assert captured["limit"] == 7
    assert lines == [
        "2026-08-02T00:00:00Z | SUCCESS | job-1 | owner/repository#42 | "
        "target=src/module.py | run=run-1 | pr=-"
    ]


def test_query_memory_lines_preserves_filters_and_optional_formatting(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Store:
        def list_memory(self, **options):
            captured.update(options)
            return [
                TrajectoryMemoryRecord(
                    memory_id="memory-1",
                    run_id="run-1",
                    repo_name="owner/repository",
                    target_path="module.py",
                    status="SUCCESS",
                    lesson="Keep the public API stable.",
                    error_signature=None,
                    reward=8.125,
                    created_at=None,
                )
            ]

    lines = query_memory_lines(
        tmp_path / "runs.sqlite",
        repo_name="owner/repository",
        target_path="module.py",
        limit=9,
        store_factory=lambda path: Store(),
    )

    assert captured == {
        "repo_name": "owner/repository",
        "target_path": "module.py",
        "limit": 9,
    }
    assert lines == [
        "- | SUCCESS | owner/repository | module.py | reward=8.12 | error=-\n"
        "  Keep the public API stable."
    ]


def test_cli_queries_return_empty_lines_without_synthetic_records(tmp_path: Path) -> None:
    class Store:
        def list_github_jobs(self, limit: int = 100):
            return []

        def list_memory(self, **options):
            return []

    factory = lambda path: Store()

    assert query_job_lines(tmp_path / "runs.sqlite", 20, store_factory=factory) == []
    assert query_memory_lines(
        tmp_path / "runs.sqlite",
        repo_name=None,
        target_path=None,
        limit=20,
        store_factory=factory,
    ) == []
