from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from refactor_agent.cli_config import (
    resolve_database,
    resolve_deadline,
    resolve_github_workspace_root,
    resolve_run_root,
)


def test_resolve_run_root_uses_env_only_for_cli_default(monkeypatch, tmp_path: Path) -> None:
    env_root = tmp_path / "runs"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv("REFACTOR_AGENT_RUN_ROOT", str(env_root))

    assert resolve_run_root(Path(".runs")) == env_root
    assert resolve_run_root(explicit_root) == explicit_root


def test_resolve_database_prefers_explicit_then_environment_then_run_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_database = tmp_path / "refactor_agent.sqlite"
    explicit_database = tmp_path / "explicit.sqlite"
    run_root = tmp_path / "runs"
    monkeypatch.setenv("REFACTOR_AGENT_DATABASE", str(env_database))

    assert resolve_database(explicit_database, run_root) == explicit_database
    assert resolve_database(None, run_root) == env_database
    monkeypatch.delenv("REFACTOR_AGENT_DATABASE")
    assert resolve_database(None, run_root) == run_root / "refactor_agent.sqlite"


def test_resolve_github_workspace_uses_env_only_for_cli_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_workspace = tmp_path / "github-workspaces"
    explicit_workspace = tmp_path / "explicit"
    monkeypatch.setenv("REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT", str(env_workspace))

    assert resolve_github_workspace_root(Path(".github-url-workspaces")) == env_workspace
    assert resolve_github_workspace_root(explicit_workspace) == explicit_workspace


def test_resolve_deadline_uses_env_only_for_cli_default(monkeypatch) -> None:
    monkeypatch.setenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", "1200")

    assert resolve_deadline(900) == 1200
    assert resolve_deadline(600) == 600


def test_resolve_default_deadline_keeps_app_settings_bounds(monkeypatch) -> None:
    monkeypatch.setenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", "29")

    with pytest.raises(ValidationError):
        resolve_deadline(900)
