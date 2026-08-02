from __future__ import annotations

from typer.testing import CliRunner

from refactor_agent.cli import app
from refactor_agent.dashboard_launcher import (
    DashboardDependencyError,
    DashboardLaunchResult,
)


runner = CliRunner()


def test_dashboard_cli_preserves_ready_output_and_child_exit(monkeypatch) -> None:
    def fake_launch(**options):
        options["on_ready"]("http://127.0.0.1:8765")
        return DashboardLaunchResult(url="http://127.0.0.1:8765", returncode=7)

    monkeypatch.setattr("refactor_agent.cli.launch_dashboard", fake_launch)
    result = runner.invoke(app, ["dashboard", "--port", "8765"])

    assert result.exit_code == 7
    assert "Arena URL: http://127.0.0.1:8765" in result.stdout


def test_dashboard_cli_preserves_missing_dependency_exit(monkeypatch) -> None:
    def fail(**options):
        raise DashboardDependencyError(
            "Streamlit is not installed. Install it with: "
            "python -m pip install -e .[dashboard]"
        )

    monkeypatch.setattr("refactor_agent.cli.launch_dashboard", fail)
    result = runner.invoke(app, ["dashboard"])

    assert result.exit_code == 1
    assert "Streamlit is not installed" in result.stdout
