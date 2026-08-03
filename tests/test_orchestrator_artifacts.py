import json
from pathlib import Path

from refactor_agent.models import AdversarialTestResult, MutationTestResult, SandboxResult
from refactor_agent.orchestrator_artifacts import write_run_artifacts


def test_write_run_artifacts_persists_complete_artifact_set(tmp_path: Path):
    write_run_artifacts(
        tmp_path,
        "run-1",
        {
            "original_code": "def value():\n    return 1\n",
            "current_code": "def value():\n    return 2\n",
            "sandbox": SandboxResult(
                passed=True,
                returncode=0,
                stdout="3 passed",
                stderr="pytest warning",
                duration_seconds=0.1,
            ),
            "adversarial": AdversarialTestResult(
                generated=1,
                passed=False,
                returncode=1,
                stdout="counterexample",
                stderr="assertion failed",
            ),
            "mutation": MutationTestResult(
                total=2,
                killed=1,
                survived=1,
                survival_details=["mutant-2"],
            ),
        },
        "# Refactor report\n",
    )

    artifact_root = tmp_path / "run-1" / "artifacts"
    assert {path.name for path in artifact_root.iterdir()} == {
        "original.py",
        "candidate.py",
        "change.diff",
        "pytest.log",
        "adversary.log",
        "mutation.json",
        "report.md",
    }
    assert (artifact_root / "pytest.log").read_text(encoding="utf-8") == "3 passed\npytest warning"
    assert (artifact_root / "adversary.log").read_text(encoding="utf-8") == (
        "counterexample\nassertion failed"
    )
    assert json.loads((artifact_root / "mutation.json").read_text(encoding="utf-8")) == {
        "killed": 1,
        "survival_details": ["mutant-2"],
        "survived": 1,
        "total": 2,
    }
    assert (artifact_root / "report.md").read_text(encoding="utf-8") == "# Refactor report\n"

    # Atomic writers must not leave handles open after returning.
    (artifact_root / "report.md").replace(artifact_root / "renamed-report.md")


def test_write_run_artifacts_uses_original_and_empty_optional_results(tmp_path: Path):
    write_run_artifacts(
        tmp_path,
        "run-2",
        {"original_code": "value = 1\n"},
        "report",
    )

    artifact_root = tmp_path / "run-2" / "artifacts"
    assert (artifact_root / "candidate.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (artifact_root / "change.diff").read_text(encoding="utf-8") == ""
    assert (artifact_root / "pytest.log").read_text(encoding="utf-8") == ""
    assert (artifact_root / "adversary.log").read_text(encoding="utf-8") == ""
    assert json.loads((artifact_root / "mutation.json").read_text(encoding="utf-8")) == {}
