from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status

from refactor_agent.config import AppSettings
from refactor_agent.github import GitHubAutomationService
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.models import GitHubRefactorJob
from refactor_agent.store import SQLiteRunStore
from refactor_agent.sandbox import docker_status


def create_app(
    settings: AppSettings | None = None,
    service: GitHubAutomationService | None = None,
    store: SQLiteRunStore | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or AppSettings.from_env()
    service = service or GitHubAutomationService(settings)
    store = store or SQLiteRunStore(settings.resolved_database_path)
    worker = GitHubJobWorker(settings, service, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_webhook_settings(settings, require_docker=start_worker)
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="Refactor Agent Webhook", version="0.1.0", lifespan=lifespan)
    app.state.worker = worker

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/jobs")
    def list_jobs(request: Request, limit: int = 20) -> dict[str, Any]:
        _require_admin(request, settings)
        bounded_limit = max(1, min(limit, 100))
        return {
            "jobs": [
                job.model_dump(mode="json", exclude={"payload_json"})
                for job in store.list_github_jobs(bounded_limit)
            ]
        }

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        _require_admin(request, settings)
        job = store.get_github_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return job.model_dump(mode="json", exclude={"payload_json"})

    @app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> dict[str, Any]:
        try:
            content_length = int(request.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length.") from exc
        if content_length > settings.webhook_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Webhook payload too large.")
        body = await request.body()
        if len(body) > settings.webhook_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Webhook payload too large.")
        signature = request.headers.get("X-Hub-Signature-256")
        if not settings.github_webhook_secret or not verify_github_signature(
            body,
            signature,
            settings.github_webhook_secret,
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitHub signature.")

        event_name = request.headers.get("X-GitHub-Event", "")
        if event_name not in {"issues", "issue_comment"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported GitHub event.")
        delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
        if not delivery_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing GitHub delivery ID.")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload must be an object.")

        parsed = parse_github_payload(event_name, payload, settings.default_tests_path, delivery_id)
        if parsed is None:
            return {"status": "ignored", "event": event_name, "action": payload.get("action")}

        allowed_repositories = {item.lower() for item in settings.allowed_repositories}
        allowed_senders = {item.lower() for item in settings.allowed_senders}
        if parsed.repo_full_name.lower() not in allowed_repositories:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repository is not allowlisted.")
        if not parsed.sender_login or parsed.sender_login.lower() not in allowed_senders:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="GitHub sender is not authorized.")
        record = store.create_github_job(parsed)
        return {
            "status": "accepted" if record.job_id == parsed.job_id else "duplicate",
            "job_id": record.job_id,
            "repo": parsed.repo_full_name,
            "issue": parsed.issue_number,
            "target": parsed.target_path,
            "tests": parsed.tests_path,
        }

    return app


app = create_app()


def verify_github_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_github_payload(
    event_name: str,
    payload: dict[str, Any],
    default_tests_path: str = "tests",
    delivery_id: str = "local-test-delivery",
) -> GitHubRefactorJob | None:
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action", ""))
    if event_name == "issues" and action == "opened":
        issue = payload.get("issue") or {}
        if not isinstance(issue, dict):
            return None
        if issue.get("pull_request"):
            return None
        issue_text = _join_text(issue.get("title"), issue.get("body"))
    elif event_name == "issue_comment" and action == "created":
        issue = payload.get("issue") or {}
        if not isinstance(issue, dict):
            return None
        if issue.get("pull_request"):
            return None
        comment = payload.get("comment") or {}
        if not isinstance(comment, dict):
            return None
        issue_text = _join_text(issue.get("title"), issue.get("body"), comment.get("body"))
    else:
        return None

    repo = payload.get("repository") or {}
    if not isinstance(repo, dict):
        return None
    repo_full_name = str(repo.get("full_name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_full_name):
        return None
    target_path = extract_directive(issue_text, ("target", "file", "path"))
    tests_path = extract_directive(issue_text, ("tests", "test")) or default_tests_path
    try:
        target_path = normalize_repo_path(target_path) if target_path else AUTO_TARGET_PATH
        tests_path = normalize_repo_path(tests_path)
    except ValueError:
        return None

    try:
        issue_number = int(issue["number"])
    except (KeyError, TypeError, ValueError):
        return None
    return GitHubRefactorJob(
        job_id=build_job_id(repo_full_name, issue_number),
        delivery_id=delivery_id,
        repo_full_name=repo_full_name,
        default_branch=str(repo.get("default_branch") or "main"),
        issue_number=issue_number,
        issue_title=str(issue.get("title") or f"Issue #{issue['number']}"),
        issue_text=issue_text,
        target_path=target_path,
        tests_path=tests_path,
        sender_login=(payload.get("sender") or {}).get("login"),
        event_name=event_name,
        action=action,
    )


def build_job_id(repo_full_name: str, issue_number: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo_full_name).strip("_") or "repo"
    return f"{safe_repo}__issue-{issue_number}__{stamp}-{uuid4().hex[:8]}"


def validate_webhook_settings(settings: AppSettings, require_docker: bool = False) -> None:
    missing = []
    if not settings.github_webhook_secret:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not settings.admin_token:
        missing.append("REFACTOR_AGENT_ADMIN_TOKEN")
    if not settings.allowed_repositories:
        missing.append("REFACTOR_AGENT_ALLOWED_REPOSITORIES")
    if not settings.allowed_senders:
        missing.append("REFACTOR_AGENT_ALLOWED_SENDERS")
    if not settings.dry_run and not settings.github_token:
        missing.append("GITHUB_TOKEN")
    if not settings.mock_llm and not os.getenv("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        raise RuntimeError("Webhook configuration is fail-closed; missing: " + ", ".join(missing))
    if settings.sandbox_backend != "docker":
        raise RuntimeError("Webhook mode requires REFACTOR_AGENT_SANDBOX_BACKEND=docker.")
    if require_docker:
        docker = docker_status()
        if not docker.available:
            raise RuntimeError(f"Webhook mode requires an available Docker daemon: {docker.error}")


def _require_admin(request: Request, settings: AppSettings) -> None:
    expected = f"Bearer {settings.admin_token or ''}"
    provided = request.headers.get("Authorization", "")
    if not settings.admin_token or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token.")


def extract_directive(text: str, names: tuple[str, ...]) -> str | None:
    names_pattern = "|".join(re.escape(name) for name in names)
    patterns = [
        rf"(?im)^\s*(?:{names_pattern})\s*[:=]\s*`?([^`\s]+)`?\s*$",
        rf"(?im)\[(?:{names_pattern})\s*:\s*([^\]\s]+)\]",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def normalize_repo_path(value: str) -> str:
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not str(path):
        raise ValueError(f"Unsafe repository path: {value}")
    return str(path)


def _join_text(*parts: str | None) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())
