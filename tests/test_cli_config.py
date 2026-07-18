from pathlib import Path

from typer.testing import CliRunner

from refactor_agent.config import AppSettings
from refactor_agent.cli import (
    _resolve_database,
    _resolve_deadline,
    _resolve_github_workspace_root,
    _resolve_run_root,
    app,
)


runner = CliRunner()


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


def test_settings_read_default_and_configured_job_deadline(monkeypatch):
    monkeypatch.delenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", raising=False)
    assert AppSettings.from_env().job_deadline_seconds == 900

    monkeypatch.setenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", "1200")
    assert AppSettings.from_env().job_deadline_seconds == 1200


def test_removed_github_delivery_environment_has_no_runtime_settings(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "legacy-secret")
    monkeypatch.setenv("REFACTOR_AGENT_ALLOWED_SENDERS", "legacy-user")
    monkeypatch.setenv("REFACTOR_AGENT_DRY_RUN", "false")
    settings = AppSettings.from_env()
    assert not hasattr(settings, "github_token")
    assert not hasattr(settings, "github_webhook_secret")
    assert not hasattr(settings, "allowed_senders")
    assert not hasattr(settings, "dry_run")


def test_resolve_deadline_uses_env_only_for_default(monkeypatch):
    monkeypatch.setenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", "1200")
    assert _resolve_deadline(900) == 1200
    assert _resolve_deadline(600) == 600


def test_run_cli_exposes_bounded_deadline_option():
    help_result = runner.invoke(app, ["run", "--help"])
    assert help_result.exit_code == 0
    assert "--deadline" in help_result.stdout

    invalid = runner.invoke(app, ["run", "--deadline", "29"])
    assert invalid.exit_code == 2
    assert "30" in invalid.stderr


def test_snippet_cli_reviews_stdin_without_executing(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "snippet",
            "--source",
            "-",
            "--mode",
            "review",
            "--persona",
            "tsundere",
            "--run-root",
            str(tmp_path / "runs"),
            "--database",
            str(tmp_path / "runs.sqlite"),
        ],
        input="def add(a, b):\n    return a + b\n",
    )
    assert result.exit_code == 0
    assert "REVIEWED" in result.stdout
    assert "未执行、未验证" in result.stdout
