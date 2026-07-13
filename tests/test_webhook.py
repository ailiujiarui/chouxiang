import hashlib
import hmac
import json

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from refactor_agent.config import AppSettings
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.models import GitHubAutomationResult, GitHubRefactorJob
from refactor_agent.store import SQLiteRunStore
from refactor_agent.webhook import create_app, parse_github_payload, verify_github_signature


class CapturingService:
    def __init__(self) -> None:
        self.jobs: list[GitHubRefactorJob] = []

    def process(self, job: GitHubRefactorJob) -> GitHubAutomationResult:
        self.jobs.append(job)
        return GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            branch_name="refactor-agent/issue-42",
            run_id="run-42",
            status="DRY_RUN",
            workspace_path=Path("workspace"),
        )


def test_verify_github_signature_accepts_valid_signature():
    body = b'{"ok": true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, signature, "secret") is True


def test_verify_github_signature_rejects_invalid_signature():
    assert verify_github_signature(b"{}", "sha256=bad", "secret") is False


def test_parse_github_issue_payload_with_directives():
    payload = _issue_payload("target: src/app.py\ntests: tests/unit")
    job = parse_github_payload("issues", payload)
    assert job is not None
    assert job.repo_full_name == "octo/demo"
    assert job.job_id.startswith("octo__demo__issue-42__")
    assert job.target_path == "src/app.py"
    assert job.tests_path == "tests/unit"
    assert not hasattr(job, "clone_url")


def test_parse_github_payload_accepts_missing_target_for_auto_location():
    payload = _issue_payload("no directives here")
    job = parse_github_payload("issues", payload)
    assert job is not None
    assert job.target_path == AUTO_TARGET_PATH


def test_webhook_accepts_signed_request_and_tracks_job(tmp_path: Path):
    service = CapturingService()
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    settings = _secure_settings()
    app = create_app(settings=settings, service=service, store=store, start_worker=False)  # type: ignore[arg-type]
    body = json.dumps(_issue_payload("[target:src/app.py]")).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-42",
                "X-Hub-Signature-256": signature,
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        job_id = response.json()["job_id"]
        assert app.state.worker.run_once() is True
        assert service.jobs[0].target_path == "src/app.py"
        admin = {"Authorization": "Bearer admin-secret"}
        job_response = client.get(f"/jobs/{job_id}", headers=admin)
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "DRY_RUN"
        list_response = client.get("/jobs", headers=admin)
        assert list_response.status_code == 200
        assert client.get("/jobs").status_code == 401


def test_webhook_rejects_bad_signature(tmp_path: Path):
    service = CapturingService()
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(settings=_secure_settings(), service=service, store=store, start_worker=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/webhook/github",
            json=_issue_payload("target: src/app.py"),
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "bad", "X-Hub-Signature-256": "sha256=bad"},
        )
        assert response.status_code == 401
        assert service.jobs == []


def test_webhook_rejects_non_allowlisted_and_oversized_payloads(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    service = CapturingService()
    settings = _secure_settings(webhook_max_bytes=1024)
    app = create_app(settings=settings, service=service, store=store, start_worker=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=b"x" * 1025, headers={"Content-Length": "1025"})
        assert response.status_code == 413

        body = json.dumps(_issue_payload("target: src/app.py")).encode("utf-8")
        payload = json.loads(body)
        payload["repository"]["full_name"] = "other/repo"
        body = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "other-repo",
                "X-Hub-Signature-256": signature,
            },
        )
        assert response.status_code == 403

        payload = _issue_payload("target: src/app.py")
        payload["sender"]["login"] = "outsider"
        body = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "outsider",
                "X-Hub-Signature-256": signature,
            },
        )
        assert response.status_code == 403


def test_webhook_settings_fail_closed(tmp_path: Path):
    app = create_app(
        settings=AppSettings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )
    with pytest.raises(RuntimeError, match="fail-closed"):
        with TestClient(app):
            pass


def test_worker_marks_corrupt_durable_payload_failed_and_can_restart(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = parse_github_payload("issues", _issue_payload("target: src/app.py"), delivery_id="corrupt")
    assert job is not None
    store.create_github_job(job)
    with store._connect() as connection:
        connection.execute("UPDATE github_jobs SET payload_json = '{' WHERE job_id = ?", (job.job_id,))
    worker = GitHubJobWorker(_secure_settings(), CapturingService(), store, poll_seconds=0.01)  # type: ignore[arg-type]

    assert worker.run_once() is True
    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "FAILED"
    assert "invalid durable job payload" in (record.error or "")

    worker.start()
    worker.stop()
    worker.start()
    assert worker._stop.is_set() is False
    worker.stop()


def _issue_payload(body: str):
    return {
        "action": "opened",
        "repository": {
            "full_name": "octo/demo",
            "clone_url": "https://github.com/octo/demo.git",
            "default_branch": "main",
        },
        "issue": {
            "number": 42,
            "title": "Leap year bug",
            "body": body,
        },
        "sender": {"login": "tester"},
    }


def _secure_settings(**updates) -> AppSettings:
    values = {
        "github_webhook_secret": "secret",
        "admin_token": "admin-secret",
        "allowed_repositories": {"octo/demo"},
        "allowed_senders": {"tester"},
        "sandbox_backend": "docker",
        "dry_run": True,
        "mock_llm": True,
    }
    values.update(updates)
    return AppSettings(**values)
