from pathlib import Path

import pytest

from refactor_agent.sandbox import (
    build_docker_command,
    DockerStatus,
    prepare_workspace,
    run_performance_profile,
    run_pytest,
    run_pytest_with_backend,
    SandboxUnavailableError,
    write_candidate,
    _sandbox_env,
)


def test_sandbox_detects_passing_tests(tmp_path: Path):
    project = _make_project(tmp_path, "def add(a, b):\n    return a + b\n")
    workspace = tmp_path / "workspace"
    _, target, tests = prepare_workspace(project / "maths.py", project / "tests", workspace)
    result = run_pytest(workspace, tests, timeout_seconds=10)
    assert result.passed is True


def test_sandbox_detects_assertion_failure(tmp_path: Path):
    project = _make_project(tmp_path, "def add(a, b):\n    return a - b\n")
    workspace = tmp_path / "workspace"
    _, target, tests = prepare_workspace(project / "maths.py", project / "tests", workspace)
    result = run_pytest(workspace, tests, timeout_seconds=10)
    assert result.passed is False
    assert result.returncode != 0


def test_write_candidate_allows_retry(tmp_path: Path):
    project = _make_project(tmp_path, "def add(a, b):\n    return a - b\n")
    workspace = tmp_path / "workspace"
    _, target, tests = prepare_workspace(project / "maths.py", project / "tests", workspace)
    write_candidate(target, "def add(a, b):\n    return a + b\n")
    result = run_pytest(workspace, tests, timeout_seconds=10)
    assert result.passed is True


def test_performance_profile_reports_time_and_memory(tmp_path: Path):
    project = _make_project(tmp_path, "def add(a, b):\n    return a + b\n")
    workspace = tmp_path / "workspace"
    _, target, tests = prepare_workspace(project / "maths.py", project / "tests", workspace)
    result = run_performance_profile(workspace, target, tests, timeout_seconds=10)
    assert result.passed is True
    assert result.pytest_duration_seconds > 0
    assert result.peak_memory_kib > 0
    assert result.import_time_seconds is not None


def test_build_docker_command_uses_network_and_resource_limits(tmp_path: Path):
    command = build_docker_command(
        workspace=tmp_path,
        docker_image="refactor-agent-sandbox:py312",
        memory="128m",
        cpus=0.5,
        inner_command="print('ok')",
    )
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--network" in command
    assert "none" in command
    assert "--memory" in command
    assert "128m" in command
    assert "--cpus" in command
    assert "0.5" in command
    assert "refactor-agent-sandbox:py312" in command
    assert "--read-only" in command
    assert ["--cap-drop", "ALL"] == command[command.index("--cap-drop") : command.index("--cap-drop") + 2]
    assert "no-new-privileges" in command
    assert "--pids-limit" in command
    assert "--user" in command
    assert command[command.index("-v") + 1].endswith(":/workspace:ro")


def test_subprocess_environment_drops_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    env = _sandbox_env()
    assert "GITHUB_TOKEN" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "GITHUB_WEBHOOK_SECRET" not in env


def test_auto_backend_falls_back_to_subprocess_when_docker_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _make_project(tmp_path, "def add(a, b):\n    return a + b\n")
    workspace = tmp_path / "workspace"
    _, target, tests = prepare_workspace(project / "maths.py", project / "tests", workspace)
    monkeypatch.setattr(
        "refactor_agent.sandbox.docker_status",
        lambda: DockerStatus(available=False, error="virtualization missing"),
    )
    result = run_pytest_with_backend(workspace, tests, timeout_seconds=10, backend="auto")
    assert result.passed is True


def test_docker_backend_fails_fast_when_docker_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "refactor_agent.sandbox.docker_status",
        lambda: DockerStatus(available=False, executable="docker", error="virtualization missing"),
    )
    with pytest.raises(SandboxUnavailableError, match="virtualization missing"):
        run_pytest_with_backend(tmp_path, tmp_path, backend="docker")


def test_unknown_sandbox_backend_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        run_pytest_with_backend(tmp_path, tmp_path, backend="spaceship")


def _make_project(tmp_path: Path, code: str) -> Path:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "maths.py").write_text(code, encoding="utf-8")
    (tests / "test_maths.py").write_text(
        "from maths import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return project
