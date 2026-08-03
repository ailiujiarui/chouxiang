from __future__ import annotations

import os
from pathlib import Path

from refactor_agent.config import AppSettings


def resolve_run_root(run_root: Path) -> Path:
    env_run_root = os.getenv("REFACTOR_AGENT_RUN_ROOT")
    if env_run_root and run_root == Path(".runs"):
        return Path(env_run_root)
    return run_root


def resolve_database(database: Path | None, run_root: Path) -> Path:
    if database is not None:
        return database
    env_database = os.getenv("REFACTOR_AGENT_DATABASE")
    if env_database:
        return Path(env_database)
    return run_root / "refactor_agent.sqlite"


def resolve_github_workspace_root(github_workspace_root: Path) -> Path:
    env_workspace = os.getenv("REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT")
    if env_workspace and github_workspace_root == Path(".github-url-workspaces"):
        return Path(env_workspace)
    return github_workspace_root


def resolve_deadline(deadline_seconds: int) -> int:
    if deadline_seconds == 900:
        return AppSettings(
            job_deadline_seconds=int(os.getenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", "900"))
        ).job_deadline_seconds
    return deadline_seconds
