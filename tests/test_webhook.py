import hashlib
import hmac
import json

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from refactor_agent.config import AppSettings
from refactor_agent.artifacts import RunArtifactWriter
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.models import (
    BenchmarkCaseRecord,
    BenchmarkRunRecord,
    GitHubAutomationResult,
    GitHubRefactorJob,
    RepositoryJobKind,
    RunRecord,
)
from refactor_agent.repository_allowlist import RepositoryAllowlistPolicy
from refactor_agent.store import SQLiteRunStore
from refactor_agent.webhook import (
    create_app,
    parse_github_payload,
    parse_github_repository_url,
    verify_github_signature,
)


class CapturingService:
    def __init__(self) -> None:
        self.jobs: list[GitHubRefactorJob] = []

    def process(self, job: GitHubRefactorJob, execution_control=None) -> GitHubAutomationResult:
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


class CapturingLocalService:
    def __init__(self) -> None:
        self.jobs: list[GitHubRefactorJob] = []

    def process(self, job: GitHubRefactorJob, execution_control=None) -> GitHubAutomationResult:
        self.jobs.append(job)
        return GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=None,
            run_id="run-url-1",
            status="DRY_RUN",
        )


def test_verify_github_signature_accepts_valid_signature():
    body = b'{"ok": true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, signature, "secret") is True


def test_verify_github_signature_rejects_invalid_signature():
    assert verify_github_signature(b"{}", "sha256=bad", "secret") is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Octo/Demo", "Octo/Demo"),
        ("https://github.com/octo/demo.git", "octo/demo"),
        ("https://github.com/octo/demo/", "octo/demo"),
    ],
)
def test_parse_github_repository_url_accepts_canonical_https(url: str, expected: str):
    assert parse_github_repository_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/octo/demo",
        "https://example.com/octo/demo",
        "https://api.github.com/octo/demo",
        "https://user:token@github.com/octo/demo",
        "https://github.com:8443/octo/demo",
        "https://github.com/octo/demo?ref=main",
        "https://github.com/octo/demo#readme",
        "https://github.com/octo/demo/issues",
        "git@github.com:octo/demo.git",
    ],
)
def test_parse_github_repository_url_rejects_unsafe_urls(url: str):
    with pytest.raises(ValueError):
        parse_github_repository_url(url)


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
        assert client.get("/jobs").status_code == 200


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


def test_worker_dispatches_dashboard_url_job_to_local_service(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    automation = CapturingService()
    local = CapturingLocalService()
    job = GitHubRefactorJob(
        job_kind=RepositoryJobKind.DASHBOARD_URL,
        job_id="url-job-1",
        delivery_id="dashboard:delivery-1",
        repo_full_name="octo/demo",
        default_branch=None,
        issue_number=None,
        issue_title="Dashboard URL 本地简化任务",
        issue_text="简化 calculate 函数",
        target_path=AUTO_TARGET_PATH,
        tests_path="tests",
        event_name="dashboard_url",
        action="submitted",
    )
    store.create_github_job(job)
    worker = GitHubJobWorker(
        _secure_settings(dry_run=False),
        automation,
        store,
        local_service=local,
    )  # type: ignore[arg-type]

    assert worker.run_once() is True
    assert automation.jobs == []
    assert [item.job_id for item in local.jobs] == [job.job_id]
    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "DRY_RUN"
    assert record.run_id == "run-url-1"


def test_worker_rechecks_removed_repository_before_dispatch(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    settings = _secure_settings(allowed_repositories=set())
    policy = RepositoryAllowlistPolicy(settings, store)
    policy.add("octo/demo")
    job = parse_github_payload("issues", _issue_payload("target: src/app.py"), delivery_id="revoked")
    assert job is not None
    store.create_github_job(job)
    assert policy.remove("octo/demo") is True
    service = CapturingService()
    worker = GitHubJobWorker(
        settings,
        service,  # type: ignore[arg-type]
        store,
        repository_policy=policy,
    )

    assert worker.run_once() is True

    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "FAILED"
    assert "allowlist" in (record.error or "").lower()
    assert service.jobs == []


def test_admin_can_cancel_retry_and_read_job_events(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = parse_github_payload("issues", _issue_payload("target: src/app.py"), delivery_id="control")
    assert job is not None
    store.create_github_job(job)
    app = create_app(
        settings=_secure_settings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=store,
        start_worker=False,
    )
    admin = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        assert client.post(f"/jobs/{job.job_id}/cancel").status_code == 401
        cancelled = client.post(f"/jobs/{job.job_id}/cancel", headers=admin)
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "CANCELLED"

        repeated = client.post(f"/jobs/{job.job_id}/cancel", headers=admin)
        assert repeated.status_code == 409

        events = client.get(f"/jobs/{job.job_id}/events", headers=admin)
        assert events.status_code == 200
        assert [event["to_status"] for event in events.json()["events"]] == ["QUEUED", "CANCELLED"]

        retried = client.post(f"/jobs/{job.job_id}/retry", headers=admin)
        assert retried.status_code == 202
        assert retried.json()["status"] == "QUEUED"


def test_admin_can_submit_allowlisted_dashboard_url_job(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(
        settings=_secure_settings(dry_run=False, github_token="github-read-token"),
        service=CapturingService(),  # type: ignore[arg-type]
        store=store,
        start_worker=False,
    )
    body = {
        "repository_url": "https://github.com/octo/demo.git",
        "refactor_request": "简化 calculate 函数并保持公开行为不变",
        "branch": None,
        "target_path": None,
        "tests_path": "tests",
    }

    with TestClient(app) as client:
        assert client.post("/jobs/url", json=body).status_code == 401
        response = client.post(
            "/jobs/url",
            json=body,
            headers={"Authorization": "Bearer admin-secret"},
        )
        capabilities = client.get("/capabilities")

    assert response.status_code == 202
    payload = response.json()
    assert payload["job_kind"] == "DASHBOARD_URL"
    assert payload["repo_full_name"] == "octo/demo"
    assert payload["issue_number"] is None
    assert payload["target_path"] == AUTO_TARGET_PATH
    record = store.get_github_job(payload["job_id"])
    assert record is not None
    assert record.job_kind == RepositoryJobKind.DASHBOARD_URL
    assert "repository_url" not in (record.payload_json or "")
    assert capabilities.json() == {
        "sandbox_backend": "docker",
        "graph_backend": "langgraph",
        "llm_mode": "mock",
        "url_submission": True,
    }


def test_admin_manages_persisted_repository_allowlist(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(
        settings=_secure_settings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=store,
        start_worker=False,
    )
    admin = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        assert client.get("/admin/repository-allowlist").status_code == 401
        added = client.post(
            "/admin/repository-allowlist",
            json={"repository": "https://github.com/Other/Repo"},
            headers=admin,
        )
        entries = client.get("/admin/repository-allowlist", headers=admin)
        protected = client.delete(
            "/admin/repository-allowlist/octo/demo",
            headers=admin,
        )
        removed = client.delete(
            "/admin/repository-allowlist/other/repo",
            headers=admin,
        )
        repeated = client.delete(
            "/admin/repository-allowlist/other/repo",
            headers=admin,
        )

    assert added.status_code == 200
    assert added.json()["repo_full_name"] == "other/repo"
    assert [(item["repo_full_name"], item["source"], item["removable"]) for item in entries.json()["entries"]] == [
        ("octo/demo", "ENVIRONMENT", False),
        ("other/repo", "DASHBOARD", True),
    ]
    assert protected.status_code == 409
    assert removed.json() == {"repo_full_name": "other/repo", "removed": True}
    assert repeated.json() == {"repo_full_name": "other/repo", "removed": False}


def test_allowlist_api_authenticates_before_parsing_and_rejects_unsafe_values(tmp_path: Path):
    app = create_app(
        settings=_secure_settings(webhook_max_bytes=1024),
        service=CapturingService(),  # type: ignore[arg-type]
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )
    admin = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        unauthenticated = client.post(
            "/admin/repository-allowlist",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        oversized = client.post(
            "/admin/repository-allowlist",
            content=b"x" * 1025,
            headers={**admin, "Content-Type": "application/json", "Content-Length": "1025"},
        )
        unsafe = client.post(
            "/admin/repository-allowlist",
            json={"repository": "https://example.com/octo/demo"},
            headers=admin,
        )

    assert unauthenticated.status_code == 401
    assert oversized.status_code == 413
    assert unsafe.status_code == 400


def test_url_submission_observes_allowlist_changes_without_restart(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(
        settings=_secure_settings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=store,
        start_worker=False,
    )
    admin = {"Authorization": "Bearer admin-secret"}
    body = {
        "repository_url": "https://github.com/other/repo",
        "refactor_request": "简化 calculate 函数",
        "tests_path": "tests",
    }

    with TestClient(app) as client:
        assert client.post(
            "/admin/repository-allowlist",
            json={"repository": "other/repo"},
            headers=admin,
        ).status_code == 200
        assert client.post("/jobs/url", json=body, headers=admin).status_code == 202
        assert client.delete(
            "/admin/repository-allowlist/other/repo",
            headers=admin,
        ).status_code == 200
        assert client.post("/jobs/url", json=body, headers=admin).status_code == 403


def test_webhook_observes_allowlist_changes_without_restart(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    app = create_app(
        settings=_secure_settings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=store,
        start_worker=False,
    )
    admin = {"Authorization": "Bearer admin-secret"}
    payload = _issue_payload("target: src/app.py")
    payload["repository"]["full_name"] = "other/repo"
    body = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        client.post(
            "/admin/repository-allowlist",
            json={"repository": "other/repo"},
            headers=admin,
        )
        accepted = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "dynamic-allowlist-1",
                "X-Hub-Signature-256": signature,
            },
        )
        client.delete("/admin/repository-allowlist/other/repo", headers=admin)
        rejected = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "dynamic-allowlist-2",
                "X-Hub-Signature-256": signature,
            },
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 403


def test_dashboard_url_submission_authenticates_before_parsing_and_limits_body(tmp_path: Path):
    app = create_app(
        settings=_secure_settings(webhook_max_bytes=1024),
        service=CapturingService(),  # type: ignore[arg-type]
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/jobs/url",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        oversized = client.post(
            "/jobs/url",
            content=b"x" * 1025,
            headers={
                "Authorization": "Bearer admin-secret",
                "Content-Type": "application/json",
                "Content-Length": "1025",
            },
        )

    assert unauthenticated.status_code == 401
    assert oversized.status_code == 413


@pytest.mark.parametrize(
    ("body_update", "status_code"),
    [
        ({"repository_url": "https://github.com/other/repo"}, 403),
        ({"repository_url": "https://user:token@github.com/octo/demo"}, 400),
        ({"target_path": "../secret.py"}, 400),
        ({"target_path": "src/readme.md"}, 400),
        ({"tests_path": "/tmp/tests"}, 400),
        ({"branch": "feature..bad"}, 400),
        ({"refactor_request": "   "}, 400),
    ],
)
def test_dashboard_url_submission_rejects_invalid_input(
    tmp_path: Path,
    body_update: dict[str, object],
    status_code: int,
):
    app = create_app(
        settings=_secure_settings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=SQLiteRunStore(tmp_path / "runs.sqlite"),
        start_worker=False,
    )
    body = {
        "repository_url": "https://github.com/octo/demo",
        "refactor_request": "简化 calculate 函数",
        "branch": None,
        "target_path": None,
        "tests_path": "tests",
    }
    body.update(body_update)

    with TestClient(app) as client:
        response = client.post(
            "/jobs/url",
            json=body,
            headers={"Authorization": "Bearer admin-secret"},
        )

    assert response.status_code == status_code


def test_job_read_endpoints_are_read_only_without_admin_token(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = parse_github_payload("issues", _issue_payload("target: src/app.py"), delivery_id="read-only")
    assert job is not None
    store.create_github_job(job)
    app = create_app(
        settings=_secure_settings(),
        service=CapturingService(),  # type: ignore[arg-type]
        store=store,
        start_worker=False,
    )

    with TestClient(app) as client:
        assert client.get("/jobs").status_code == 200
        assert client.get(f"/jobs/{job.job_id}").status_code == 200
        assert client.get(f"/jobs/{job.job_id}/events").status_code == 200
        assert client.post(f"/jobs/{job.job_id}/cancel").status_code == 401


def test_read_api_exposes_runs_artifacts_and_benchmarks_without_secrets(tmp_path: Path):
    run_root = tmp_path / "runs"
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    store.save(
        RunRecord(
            run_id="run-1",
            repo_name="octo/demo",
            self_heal_count=0,
            status="FAILED",
            error="Bearer abcdefghijklmnopqrstuvwxyz",
        )
    )
    trajectory = run_root / "run-1" / "trajectory.jsonl"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text('{"attempt":1,"status":"FAILED","message":"done"}\n', encoding="utf-8")
    writer = RunArtifactWriter(run_root / "run-1")
    writer.write_sources("x = 1\n", "x = 2\n")
    writer.write_log("pytest.log", "GITHUB_TOKEN=" + "ghp_" + "a" * 32)
    writer.write_log("adversary.log", "pass")
    writer.write_json("mutation.json", {})
    writer.write_report("report")
    benchmark = BenchmarkRunRecord(
        run_id="benchmark-1",
        manifest_hash="a" * 64,
        provider="mock",
        model="deterministic-gold",
        status="SUCCESS",
        generated_at="2026-07-14T00:00:00Z",
    )
    benchmark_case = BenchmarkCaseRecord(
        run_id=benchmark.run_id,
        case_name="case",
        repository="octo/demo",
        commit="b" * 40,
        provider="mock",
        model="deterministic-gold",
        status="SUCCESS",
        expected_status="SUCCESS",
        normalized_hash="c" * 64,
    )
    store.save_benchmark_run(benchmark, [benchmark_case])
    settings = _secure_settings(run_root=run_root)
    app = create_app(settings=settings, service=CapturingService(), store=store, start_worker=False)  # type: ignore[arg-type]

    with TestClient(app) as client:
        runs = client.get("/runs")
        assert runs.status_code == 200
        assert "abcdefghijklmnopqrstuvwxyz" not in runs.text
        assert client.get("/runs/run-1/trajectory").json()["trajectory"][0]["status"] == "FAILED"
        artifact = client.get("/runs/run-1/artifacts/pytest.log")
        assert artifact.status_code == 200
        assert "ghp_" not in artifact.text
        assert client.get("/runs/run-1/artifacts/not-allowed.txt").status_code == 404
        assert client.get("/benchmarks").json()["runs"][0]["run_id"] == "benchmark-1"
        detail = client.get("/benchmarks/benchmark-1").json()
        assert detail["cases"][0]["case_name"] == "case"


class CancellingService:
    def __init__(self, store: SQLiteRunStore) -> None:
        self.store = store

    def process(self, job: GitHubRefactorJob, execution_control=None) -> GitHubAutomationResult:
        self.store.request_github_job_cancellation(job.job_id)
        return GitHubAutomationResult(
            job_id=job.job_id,
            repo_full_name=job.repo_full_name,
            issue_number=job.issue_number,
            status="SUCCESS",
            pr_url="https://github.com/octo/demo/pull/1",
        )


def test_worker_does_not_overwrite_running_cancellation_with_success(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    job = parse_github_payload("issues", _issue_payload("target: src/app.py"), delivery_id="cancel-running")
    assert job is not None
    store.create_github_job(job)
    worker = GitHubJobWorker(_secure_settings(), CancellingService(store), store)  # type: ignore[arg-type]

    assert worker.run_once() is True

    record = store.get_github_job(job.job_id)
    assert record is not None
    assert record.status == "CANCELLED"
    assert record.pr_url is None
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
