import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from refactor_agent.config import AppSettings
from refactor_agent.models import GitHubRefactorJob
from refactor_agent.webhook import create_app, parse_github_payload, verify_github_signature


class CapturingService:
    def __init__(self) -> None:
        self.jobs: list[GitHubRefactorJob] = []

    def process(self, job: GitHubRefactorJob) -> None:
        self.jobs.append(job)


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
    assert job.target_path == "src/app.py"
    assert job.tests_path == "tests/unit"


def test_parse_github_payload_ignores_missing_target():
    payload = _issue_payload("no directives here")
    assert parse_github_payload("issues", payload) is None


def test_webhook_accepts_signed_request_and_queues_job():
    service = CapturingService()
    settings = AppSettings(github_webhook_secret="secret", dry_run=True, mock_llm=True)
    app = create_app(settings=settings, service=service)  # type: ignore[arg-type]
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
    assert service.jobs[0].target_path == "src/app.py"


def test_webhook_rejects_bad_signature():
    service = CapturingService()
    settings = AppSettings(github_webhook_secret="secret", dry_run=True, mock_llm=True)
    app = create_app(settings=settings, service=service)  # type: ignore[arg-type]
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
