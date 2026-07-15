from pathlib import Path

from refactor_agent.cli import _resolve_database, _resolve_github_workspace_root, _resolve_run_root


def test_resolve_run_root_uses_env_for_default(monkeypatch, tmp_path: Path):
    env_root = tmp_path / "runs"
    monkeypatch.setenv("REFACTOR_AGENT_RUN_ROOT", str(env_root))

    assert _resolve_run_root(Path(".runs")) == env_root
    assert _resolve_run_root(tmp_path / "explicit") == tmp_path / "explicit"


def test_resolve_database_uses_env_unless_explicit(monkeypatch, tmp_path: Path):
    env_database = tmp_path / "refactor_agent.sqlite"
    explicit_database = tmp_path / "explicit.sqlite"
    monkeypatch.setenv("REFACTOR_AGENT_DATABASE", str(env_database))

    assert _resolve_database(None, tmp_path / "runs") == env_database
    assert _resolve_database(explicit_database, tmp_path / "runs") == explicit_database


def test_resolve_github_workspace_root_uses_env_for_default(monkeypatch, tmp_path: Path):
    env_workspace = tmp_path / "github-workspaces"
    monkeypatch.setenv("REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT", str(env_workspace))

    assert _resolve_github_workspace_root(Path(".github-url-workspaces")) == env_workspace
    assert _resolve_github_workspace_root(tmp_path / "explicit") == tmp_path / "explicit"
