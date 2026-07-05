from pathlib import Path

from refactor_agent.sandbox import prepare_workspace, run_pytest, write_candidate


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
