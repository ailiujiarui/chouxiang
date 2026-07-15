from pathlib import Path

import pytest

from refactor_agent.artifacts import RunArtifactWriter, resolve_artifact_path, sanitize_text
from refactor_agent.models import TrajectoryStep
from refactor_agent.trajectory import append_trajectory


def test_artifact_writer_creates_fixed_redacted_utf8_artifacts(tmp_path: Path):
    writer = RunArtifactWriter(tmp_path / "run-1")
    writer.write_sources(
        "def value():\n    return 1\n",
        "def value():\n    return 2\n",
    )
    writer.write_log("pytest.log", "Bearer secret-token\nGITHUB_TOKEN=" + "ghp_" + "a" * 32)
    writer.write_log("adversary.log", "passed")
    writer.write_json("mutation.json", {"total": 1, "killed": 1})
    writer.write_report("# Report")

    assert {path.name for path in writer.artifact_root.iterdir()} == {
        "original.py",
        "candidate.py",
        "change.diff",
        "pytest.log",
        "adversary.log",
        "mutation.json",
        "report.md",
    }
    pytest_log = (writer.artifact_root / "pytest.log").read_text(encoding="utf-8")
    assert "secret-token" not in pytest_log
    assert "ghp_" not in pytest_log
    assert "[REDACTED]" in pytest_log


def test_artifact_writer_caps_logs_without_splitting_utf8(tmp_path: Path):
    writer = RunArtifactWriter(tmp_path / "run-1", max_log_bytes=17)

    writer.write_log("pytest.log", "测试" * 20)

    payload = (writer.artifact_root / "pytest.log").read_bytes()
    assert len(payload) <= 17
    payload.decode("utf-8")


@pytest.mark.parametrize(
    "secret",
    [
        "DEEPSEEK_API_KEY=" + "sk-" + "a" * 26,
        "GITHUB_WEBHOOK_SECRET=webhook-value",
        "REFACTOR_AGENT_ADMIN_TOKEN=admin-value",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_sanitize_text_redacts_supported_credentials(secret: str):
    sanitized = sanitize_text(secret)
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized


def test_resolve_artifact_path_rejects_traversal_and_symlink_escape(tmp_path: Path):
    run_root = tmp_path / "runs"
    artifact_root = run_root / "run-1" / "artifacts"
    artifact_root.mkdir(parents=True)
    (artifact_root / "report.md").write_text("ok", encoding="utf-8")

    assert resolve_artifact_path(run_root, "run-1", "report.md") == (artifact_root / "report.md").resolve()
    with pytest.raises(ValueError, match="artifact"):
        resolve_artifact_path(run_root, "../outside", "report.md")
    with pytest.raises(ValueError, match="artifact"):
        resolve_artifact_path(run_root, "run-1", "../report.md")

    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = artifact_root / "link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="artifact"):
        resolve_artifact_path(run_root, "run-1", "link.md")


def test_trajectory_writer_redacts_credentials(tmp_path: Path):
    path = tmp_path / "trajectory.jsonl"

    append_trajectory(
        path,
        TrajectoryStep(
            attempt=1,
            status="FAILED",
            message="Bearer abcdefghijklmnopqrstuvwxyz",
            metadata={"provider_error": "DEEPSEEK_API_KEY=" + "sk-" + "a" * 26},
        ),
    )

    payload = path.read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwxyz" not in payload
    assert "[REDACTED]" in payload
