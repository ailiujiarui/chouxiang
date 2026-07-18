from pathlib import Path

from refactor_agent.config import AppSettings
from refactor_agent.models import GitHubRefactorJob, RepositoryJobKind
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
        "snippet_source": "def add(a, b):\n    return a + b\n",
        "snippet_mode": "REVIEW",
        "persona": "STRICT",
    }
    values.update(updates)
    return GitHubRefactorJob(**values)


def test_review_persists_explicitly_unverified_report(tmp_path: Path):
    settings = AppSettings(run_root=tmp_path / "runs", database_path=tmp_path / "runs.sqlite")
    result = SnippetRefactorService(settings).process(_job(persona="TSUNDERE"))
    assert result.status == "DRY_RUN"
    assert result.run_id
    record = SQLiteRunStore(settings.resolved_database_path).get(result.run_id)
    assert record is not None
    assert record.status == "REVIEWED"
    report = (settings.run_root / result.run_id / "artifacts" / "report.md").read_text(encoding="utf-8")
    assert "未执行、未验证" in report
    assert "才、才不是" in report
    assert "没有 Reward" in report


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
    assert "人格点评" in report
    assert "才不是" in report
