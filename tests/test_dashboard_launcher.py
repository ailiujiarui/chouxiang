from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from refactor_agent.dashboard_launcher import DashboardDependencyError, launch_dashboard


def test_dashboard_launcher_builds_environment_and_reports_ready_before_run(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    events: list[str] = []
    original_environment = {"EXISTING": "value"}

    def run_process(command, **options):
        events.append("run")
        captured["command"] = command
        captured["environment"] = options["env"]
        return SimpleNamespace(returncode=7)

    result = launch_dashboard(
        host="127.0.0.1",
        port=8501,
        database_path=tmp_path / "runs.sqlite",
        run_root=tmp_path / "runs",
        api_url="http://api:8000",
        script_path=tmp_path / "dashboard.py",
        module_finder=lambda name: object(),
        process_runner=run_process,
        environment_factory=lambda: original_environment.copy(),
        executable="python-test",
        on_ready=lambda url: events.append(f"ready:{url}"),
    )

    assert result.url == "http://127.0.0.1:8501"
    assert result.returncode == 7
    assert events == ["ready:http://127.0.0.1:8501", "run"]
    assert captured["command"] == [
        "python-test",
        "-m",
        "streamlit",
        "run",
        str(tmp_path / "dashboard.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        "8501",
    ]
    assert captured["environment"] == {
        "EXISTING": "value",
        "REFACTOR_AGENT_DASHBOARD_DB": str(tmp_path / "runs.sqlite"),
        "REFACTOR_AGENT_RUN_ROOT": str(tmp_path / "runs"),
        "REFACTOR_AGENT_API_URL": "http://api:8000",
    }
    assert original_environment == {"EXISTING": "value"}


def test_dashboard_launcher_fails_before_environment_or_process_when_missing(
    tmp_path: Path,
) -> None:
    environment_created = False
    process_started = False

    def environment_factory():
        nonlocal environment_created
        environment_created = True
        return {}

    def process_runner(command, **options):
        nonlocal process_started
        process_started = True
        return SimpleNamespace(returncode=0)

    with pytest.raises(DashboardDependencyError, match="Streamlit is not installed"):
        launch_dashboard(
            host="127.0.0.1",
            port=8501,
            database_path=tmp_path / "runs.sqlite",
            run_root=tmp_path / "runs",
            api_url="http://api:8000",
            script_path=tmp_path / "dashboard.py",
            module_finder=lambda name: None,
            process_runner=process_runner,
            environment_factory=environment_factory,
        )

    assert environment_created is False
    assert process_started is False
