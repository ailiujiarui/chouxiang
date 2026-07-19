from pathlib import Path

from refactor_agent.config import AppSettings
from refactor_agent.models import EvidenceLevel, GitHubRefactorJob, RepositoryJobKind
from refactor_agent.snippet import SnippetRefactorService
from refactor_agent.store import SQLiteRunStore


def _job(**updates) -> GitHubRefactorJob:
    values = {
        "job_kind": RepositoryJobKind.SNIPPET,
        "job_id": "snippet-1",
        "delivery_id": "snippet:1",
        "repo_full_name": "local/snippet",
        "default_branch": None,
        "issue_number": None,
        "issue_title": "Snippet review",
        "issue_text": "审查代码",
        "target_path": "snippet.py",
        "tests_path": "test_snippet.py",
        "event_name": "snippet",
        "action": "submitted",
        "snippet_source": (
            "def add(left, right):\n"
            "    result = left + right\n"
            "    return result\n"
        ),
        "snippet_mode": "REVIEW",
        "persona": "STRICT",
    }
    values.update(updates)
    return GitHubRefactorJob(**values)


def test_review_runs_multi_agent_pipeline_with_generated_evidence(tmp_path: Path):
    settings = AppSettings(
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        mock_llm=True,
    )
    result = SnippetRefactorService(settings).process(_job(persona="TSUNDERE"))
    assert result.status == "DRY_RUN"
    assert result.run_id
    record = SQLiteRunStore(settings.resolved_database_path).get(result.run_id)
    assert record is not None
    assert record.status == "SUCCESS"
    report = (settings.run_root / result.run_id / "artifacts" / "report.md").read_text(encoding="utf-8")
    assert EvidenceLevel.GENERATED_TESTS.value in report
    assert "不能等同用户或仓库回归测试" in report
    trajectory = (settings.run_root / result.run_id / "trajectory.jsonl").read_text(encoding="utf-8")
    for status in ("MINIMIZER_PROPOSED", "DEFENDER_REVIEWED", "ADVERSARY_CRITIQUED", "JUDGE_SCORED"):
        assert status in trajectory


def test_verified_mode_rejects_missing_tests(tmp_path: Path):
    settings = AppSettings(run_root=tmp_path / "runs", database_path=tmp_path / "runs.sqlite")
    service = SnippetRefactorService(settings)
    try:
        service.process(_job(snippet_mode="VERIFIED_REFACTOR"))
    except ValueError as exc:
        assert "requires pytest source" in str(exc)
    else:
        raise AssertionError("missing tests were accepted")


def test_verified_mode_runs_full_pipeline_with_snippet_module(tmp_path: Path):
    settings = AppSettings(
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        sandbox_backend="subprocess",
        mock_llm=True,
    )
    result = SnippetRefactorService(settings).process(
        _job(
            snippet_mode="VERIFIED_REFACTOR",
            snippet_source=(
                "def add(left, right):\n"
                "    result = left + right\n"
                "    return result\n"
            ),
            snippet_tests=(
                "from snippet import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
            ),
            persona="TSUNDERE",
        )
    )
    assert result.status == "DRY_RUN"
    assert result.run_id
    record = SQLiteRunStore(settings.resolved_database_path).get(result.run_id)
    assert record is not None
    assert record.status == "SUCCESS"
    report = (settings.run_root / result.run_id / "artifacts" / "report.md").read_text(encoding="utf-8")
    assert EvidenceLevel.USER_TESTS.value in report
    assert "人格化代码审判" in report
    assert "别误会" in report
