from pathlib import Path

from fastapi.testclient import TestClient

from refactor_agent.config import AppSettings
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.models import GitHubRefactorJob, RepositoryJobKind
from refactor_agent.store import SQLiteRunStore
from refactor_agent.control_api import create_app


def _settings(tmp_path: Path, **updates) -> AppSettings:
    values = {
        "admin_token": "admin-secret",
        "allowed_repositories": {"octo/demo"},
        "sandbox_backend": "docker",
        "mock_llm": True,
        "run_root": tmp_path / "runs",
        "database_path": tmp_path / "runs.sqlite",
    }
    values.update(updates)
    return AppSettings(**values)


def test_control_api_has_no_webhook_routes(tmp_path: Path):
    app = create_app(
        settings=_settings(tmp_path),
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )
    paths = {route.path for route in app.routes}
    assert "/webhook/github" not in paths
    assert "/webhooks/github" not in paths
    assert "/jobs/url" in paths
    assert "/jobs/snippet" in paths


def test_control_api_submits_local_review_job(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(settings=_settings(tmp_path), store=store, start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            "/jobs/snippet",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "source": "def add(a, b):\n    return a + b\n",
                "refactor_request": "审查",
                "mode": "REVIEW",
                "persona": "STRICT",
            },
        )
    assert response.status_code == 202
    record = store.get_github_job(response.json()["job_id"])
    assert record is not None
    assert record.job_kind == RepositoryJobKind.SNIPPET


def test_worker_rejects_legacy_webhook_job(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_kind=RepositoryJobKind.GITHUB_WEBHOOK,
        job_id="legacy-webhook-1",
        delivery_id="legacy:1",
        repo_full_name="octo/demo",
        issue_number=42,
        issue_title="Legacy",
        issue_text="Legacy webhook",
        target_path="src/app.py",
        tests_path="tests",
        event_name="issues",
        action="opened",
    )
    store.create_github_job(job)
    worker = GitHubJobWorker(_settings(tmp_path), store)
    assert worker.run_once() is True
    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "FAILED"
    assert "removed" in (record.error or "")


def test_legacy_webhook_job_cannot_retry(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_kind=RepositoryJobKind.GITHUB_WEBHOOK,
        job_id="legacy-webhook-2",
        delivery_id="legacy:2",
        repo_full_name="octo/demo",
        issue_number=43,
        issue_title="Legacy",
        issue_text="Legacy webhook",
        target_path="src/app.py",
        tests_path="tests",
        event_name="issues",
        action="opened",
    )
    store.create_github_job(job)
    worker = GitHubJobWorker(_settings(tmp_path), store)
    worker.run_once()
    app = create_app(settings=_settings(tmp_path), store=store, start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            f"/jobs/{job.job_id}/retry",
            headers={"Authorization": "Bearer admin-secret"},
        )
    assert response.status_code == 409
