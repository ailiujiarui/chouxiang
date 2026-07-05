import hashlib
import hmac
import json

from pathlib import Path

from fastapi.testclient import TestClient

from refactor_agent.config import AppSettings
from refactor_agent.locator import AUTO_TARGET_PATH
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


def test_parse_github_payload_accepts_missing_target_for_auto_location():
    payload = _issue_payload("no directives here")
    job = parse_github_payload("issues", payload)
    assert job is not None
    assert job.target_path == AUTO_TARGET_PATH


def test_webhook_accepts_signed_request_and_tracks_job(tmp_path: Path):
    service = CapturingService()
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    settings = AppSettings(github_webhook_secret="secret", dry_run=True, mock_llm=True)
    app = create_app(settings=settings, service=service, store=store)  # type: ignore[arg-type]
    client = TestClient(app)
    body = json.dumps(_issue_payload("[target:src/app.py]")).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": signature},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    job_id = response.json()["job_id"]
    assert service.jobs[0].target_path == "src/app.py"
    job_response = client.get(f"/jobs/{job_id}")
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "DRY_RUN"
    assert job_response.json()["run_id"] == "run-42"

    list_response = client.get("/jobs")
    assert list_response.status_code == 200
    assert list_response.json()["jobs"][0]["job_id"] == job_id


def test_webhook_rejects_bad_signature(tmp_path: Path):
    service = CapturingService()
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    settings = AppSettings(github_webhook_secret="secret", dry_run=True, mock_llm=True)
    app = create_app(settings=settings, service=service, store=store)  # type: ignore[arg-type]
    client = TestClient(app)
    response = client.post(
        "/webhook/github",
        json=_issue_payload("target: src/app.py"),
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 401
    assert service.jobs == []


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
