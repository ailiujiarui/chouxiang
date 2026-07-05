from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status

from refactor_agent.config import AppSettings
from refactor_agent.github import GitHubAutomationService
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.models import GitHubRefactorJob
from refactor_agent.store import SQLiteRunStore


def create_app(
    settings: AppSettings | None = None,
    service: GitHubAutomationService | None = None,
    store: SQLiteRunStore | None = None,
) -> FastAPI:
    settings = settings or AppSettings.from_env()
    service = service or GitHubAutomationService(settings)
    store = store or SQLiteRunStore(settings.resolved_database_path)
    app = FastAPI(title="Refactor Agent Webhook", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/jobs")
    def list_jobs(limit: int = 20) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, 100))
        return {"jobs": [job.model_dump(mode="json") for job in store.list_github_jobs(bounded_limit)]}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = store.get_github_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return job.model_dump(mode="json")

    @app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if settings.github_webhook_secret and not verify_github_signature(
            body,
            signature,
            settings.github_webhook_secret,
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitHub signature.")

        event_name = request.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload.") from exc

        parsed = parse_github_payload(event_name, payload, settings.default_tests_path)
        if parsed is None:
            return {"status": "ignored", "event": event_name, "action": payload.get("action")}

        store.create_github_job(parsed)
        background_tasks.add_task(_process_and_store, service, store, parsed)
        return {
            "status": "accepted",
            "job_id": parsed.job_id,
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
) -> GitHubRefactorJob | None:
    action = str(payload.get("action", ""))
    if event_name == "issues" and action == "opened":
        issue = payload.get("issue") or {}
        if issue.get("pull_request"):
            return None
        issue_text = _join_text(issue.get("title"), issue.get("body"))
    elif event_name == "issue_comment" and action == "created":
        issue = payload.get("issue") or {}
        if issue.get("pull_request"):
            return None
        comment = payload.get("comment") or {}
        issue_text = _join_text(issue.get("title"), issue.get("body"), comment.get("body"))
    else:
        return None

    repo = payload.get("repository") or {}
    target_path = extract_directive(issue_text, ("target", "file", "path"))
    tests_path = extract_directive(issue_text, ("tests", "test")) or default_tests_path
    try:
        target_path = normalize_repo_path(target_path) if target_path else AUTO_TARGET_PATH
        tests_path = normalize_repo_path(tests_path)
    except ValueError:
        return None

    return GitHubRefactorJob(
        job_id=build_job_id(str(repo["full_name"]), int(issue["number"])),
        repo_full_name=str(repo["full_name"]),
        clone_url=str(repo.get("clone_url") or repo.get("html_url")),
        default_branch=str(repo.get("default_branch") or "main"),
        issue_number=int(issue["number"]),
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


def _process_and_store(
    service: GitHubAutomationService,
    store: SQLiteRunStore,
    job: GitHubRefactorJob,
) -> None:
    store.mark_github_job_running(job.job_id)
    try:
        result = service.process(job)
    except Exception as exc:
        store.fail_github_job(job, str(exc))
        return
    store.complete_github_job(job, result)


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
