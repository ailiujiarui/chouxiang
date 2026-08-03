from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from refactor_agent.config import AppSettings
from refactor_agent.errors import ErrorCode, public_error_message
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.models import (
    GitHubAutomationResult,
    GitHubRefactorJob,
    RepositoryJobKind,
)
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
    assert "/analysis" in paths


@pytest.mark.parametrize(
    ("mock_llm", "api_key", "deepseek_available", "llm_available"),
    [
        (False, None, False, False),
        (False, "test-key", True, True),
        (True, None, False, True),
        (True, "test-key", True, True),
    ],
)
def test_capabilities_report_llm_availability_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_llm: bool,
    api_key: str | None,
    deepseek_available: bool,
    llm_available: bool,
):
    if api_key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", api_key)
    app = create_app(
        settings=_settings(tmp_path, mock_llm=mock_llm),
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )

    with TestClient(app) as client:
        response = client.get("/capabilities")

    assert response.status_code == 200
    capabilities = response.json()
    assert capabilities["deepseek_available"] is deepseek_available
    assert capabilities["llm_available"] is llm_available
    assert capabilities["snippet_submission"] is llm_available
    assert capabilities["snippet_verified_refactor"] is llm_available
    assert capabilities["url_submission"] is llm_available


@pytest.mark.parametrize(
    ("path", "payload", "expected_detail"),
    [
        (
            "/jobs/snippet",
            {
                "source": "def add(a, b):\n    return a + b\n",
                "refactor_request": "review",
                "mode": "REVIEW",
            },
            "Task requires an available configured LLM.",
        ),
        (
            "/jobs/snippet",
            {
                "source": "def add(a, b):\n    return a + b\n",
                "tests": "from snippet import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                "refactor_request": "refactor",
                "mode": "VERIFIED_REFACTOR",
            },
            "Task requires an available configured LLM.",
        ),
        (
            "/analysis",
            {
                "input_kind": "SNIPPET",
                "instruction": "review",
                "source": "def add(a, b):\n    return a + b\n",
            },
            "Task requires an available configured LLM.",
        ),
        (
            "/analysis",
            {
                "input_kind": "SNIPPET",
                "instruction": "refactor",
                "source": "def add(a, b):\n    return a + b\n",
                "tests": "from snippet import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            },
            "Task requires an available configured LLM.",
        ),
        (
            "/jobs/url",
            {
                "repository_url": "https://github.com/octo/demo",
                "refactor_request": "refactor",
            },
            "URL submission requires Docker and an available configured LLM.",
        ),
        (
            "/analysis",
            {
                "input_kind": "REPOSITORY_URL",
                "instruction": "refactor",
                "repository_url": "https://github.com/octo/demo",
            },
            "Repository analysis requires Docker and an available configured LLM.",
        ),
    ],
)
def test_llm_task_entry_rejects_missing_deepseek_key_without_enqueuing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    payload: dict[str, object],
    expected_detail: str,
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(
        settings=_settings(tmp_path, mock_llm=False),
        store=store,
        start_worker=False,
    )

    with TestClient(app) as client:
        response = client.post(
            path,
            headers={"Authorization": "Bearer admin-secret"},
            json=payload,
        )

    assert response.status_code == 503
    assert response.json()["detail"] == expected_detail
    assert store.list_github_jobs() == []


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


def test_unified_analysis_endpoint_submits_snippet_with_declared_evidence(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(settings=_settings(tmp_path), store=store, start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            "/analysis",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "input_kind": "SNIPPET",
                "instruction": "simplify",
                "source": "def add(a, b):\n    return a + b\n",
                "persona": "TSUNDERE",
            },
        )

    assert response.status_code == 202
    assert response.json()["evidence_level"] == "STATIC"
    assert response.json()["report_persona"] == "TSUNDERE"
    assert response.json()["product_mode"] == "demo"


def test_local_single_user_mode_submits_analysis_without_admin_token(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(
        settings=_settings(tmp_path, admin_token=None),
        store=store,
        start_worker=False,
    )

    with TestClient(app) as client:
        capabilities = client.get("/capabilities")
        response = client.post(
            "/analysis",
            json={
                "input_kind": "SNIPPET",
                "instruction": "simplify",
                "source": "def add(a, b):\n    return a + b\n",
                "persona": "TSUNDERE",
            },
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["admin_token_required"] is False
    assert response.status_code == 202


def test_configured_admin_token_remains_enforced(tmp_path: Path):
    app = create_app(
        settings=_settings(tmp_path),
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )

    with TestClient(app) as client:
        capabilities = client.get("/capabilities")
        missing = client.post(
            "/analysis",
            json={
                "input_kind": "SNIPPET",
                "instruction": "simplify",
                "source": "x = 1\n",
            },
        )
        invalid = client.post(
            "/analysis",
            headers={"Authorization": "Bearer wrong"},
            json={
                "input_kind": "SNIPPET",
                "instruction": "simplify",
                "source": "x = 1\n",
            },
        )

    assert capabilities.json()["admin_token_required"] is True
    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_unified_analysis_endpoint_rejects_blank_instruction(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path), store=SQLiteRunStore(tmp_path / "runs.sqlite"), start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            "/analysis",
            headers={"Authorization": "Bearer admin-secret"},
            json={"input_kind": "SNIPPET", "instruction": "   ", "source": "x = 1"},
        )

    assert response.status_code == 400
    assert "non-whitespace" in response.json()["detail"]


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
    assert record.error is None
    assert record.error_code == ErrorCode.INTERNAL_ERROR
    assert record.error_message == public_error_message(ErrorCode.INTERNAL_ERROR)
    assert record.error_summary == "worker job failed"


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


def test_worker_can_start_and_stop_repeatedly_without_thread_leaks(tmp_path: Path):
    worker = GitHubJobWorker(
        _settings(tmp_path),
        SQLiteRunStore(tmp_path / "runs.sqlite"),
        poll_seconds=0.01,
    )

    worker_threads = []
    for _ in range(3):
        worker.start()
        assert worker._thread is not None
        worker_threads.append(worker._thread)
        worker.stop()

    assert all(not thread.is_alive() for thread in worker_threads)
    assert not any(
        thread.is_alive() and thread.name.startswith(worker.worker_id)
        for thread in threading.enumerate()
    )


def test_worker_run_closes_heartbeat_thread(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = GitHubRefactorJob(
        job_kind=RepositoryJobKind.SNIPPET,
        job_id="snippet-lifecycle",
        delivery_id="snippet:lifecycle",
        repo_full_name="local/snippet",
        issue_number=None,
        issue_title="Lifecycle",
        issue_text="review",
        target_path="snippet.py",
        tests_path="test_snippet.py",
        event_name="snippet",
        action="submitted",
        snippet_source="value = 1\n",
        snippet_mode="REVIEW",
    )
    store.create_github_job(job)
    worker = GitHubJobWorker(
        _settings(tmp_path),
        store,
        snippet_service=_SuccessfulSnippetService(),
    )

    assert worker.run_once() is True

    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "SUCCESS"
    assert not any(
        thread.is_alive() and thread.name == f"{worker.worker_id}-heartbeat"
        for thread in threading.enumerate()
    )


class _SuccessfulSnippetService:
    def process(self, job, execution_control):
        execution_control.checkpoint("test-service")
        return GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            status="SUCCESS",
        )
