from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from refactor_agent.models import GitHubJobRecord, TrajectoryMemoryRecord
from refactor_agent.store import SQLiteRunStore


class ReadOnlyQueryStore(Protocol):
    def list_github_jobs(self, limit: int = 100) -> list[GitHubJobRecord]: ...

    def list_memory(
        self,
        repo_name: str | None = None,
        target_path: str | None = None,
        limit: int = 20,
    ) -> list[TrajectoryMemoryRecord]: ...


QueryStoreFactory = Callable[[Path], ReadOnlyQueryStore]


def query_job_lines(
    database_path: Path,
    limit: int,
    *,
    store_factory: QueryStoreFactory = SQLiteRunStore,
) -> list[str]:
    records = store_factory(database_path).list_github_jobs(limit)
    return [
        (
            f"{record.updated_at} | {record.status} | {record.job_id} | "
            f"{record.repo_full_name}#{record.issue_number} | "
            f"target={record.target_path} | run={record.run_id or '-'} | "
            f"pr={record.pr_url or '-'}"
        )
        for record in records
    ]


def query_memory_lines(
    database_path: Path,
    *,
    repo_name: str | None,
    target_path: str | None,
    limit: int,
    store_factory: QueryStoreFactory = SQLiteRunStore,
) -> list[str]:
    records = store_factory(database_path).list_memory(
        repo_name=repo_name,
        target_path=target_path,
        limit=limit,
    )
    lines = []
    for record in records:
        reward = f"{record.reward:.2f}" if record.reward is not None else "-"
        signature = record.error_signature or "-"
        lines.append(
            f"{record.created_at or '-'} | {record.status} | {record.repo_name} | "
            f"{record.target_path} | reward={reward} | error={signature}\n"
            f"  {record.lesson}"
        )
    return lines
