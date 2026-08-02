from __future__ import annotations

from typer.testing import CliRunner

from refactor_agent.cli import app


runner = CliRunner()


def test_jobs_cli_preserves_lines_and_empty_message(monkeypatch) -> None:
    monkeypatch.setattr("refactor_agent.cli.query_job_lines", lambda *args, **kwargs: ["job line"])
    populated = runner.invoke(app, ["jobs"])
    assert populated.exit_code == 0
    assert populated.stdout.strip() == "job line"

    monkeypatch.setattr("refactor_agent.cli.query_job_lines", lambda *args, **kwargs: [])
    empty = runner.invoke(app, ["jobs"])
    assert empty.exit_code == 0
    assert "No GitHub jobs recorded yet." in empty.stdout


def test_memories_cli_preserves_lines_and_empty_message(monkeypatch) -> None:
    monkeypatch.setattr(
        "refactor_agent.cli.query_memory_lines",
        lambda *args, **kwargs: ["memory line\n  lesson"],
    )
    populated = runner.invoke(app, ["memories"])
    assert populated.exit_code == 0
    assert "memory line" in populated.stdout
    assert "lesson" in populated.stdout

    monkeypatch.setattr("refactor_agent.cli.query_memory_lines", lambda *args, **kwargs: [])
    empty = runner.invoke(app, ["memories"])
    assert empty.exit_code == 0
    assert "还没有轨迹记忆" in empty.stdout
