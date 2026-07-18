from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError

from refactor_agent.artifacts import resolve_artifact_path, sanitize_text
from refactor_agent.config import AppSettings
from refactor_agent.github import GitHubAutomationService
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.models import GitHubRefactorJob, RepositoryJobKind
from refactor_agent.repository_allowlist import (
    EnvironmentRepositoryRemovalError,
    RepositoryAllowlistLimitError,
    RepositoryAllowlistPolicy,
    RepositoryNotAllowlistedError,
    parse_github_repository_url,
)
from refactor_agent.store import JobTransitionError, SQLiteRunStore
from refactor_agent.sandbox import docker_status


ModelT = TypeVar("ModelT", bound=BaseModel)


class DashboardUrlJobRequest(BaseModel):
    repository_url: str
    refactor_request: str
    branch: str | None = None
    target_path: str | None = None
    tests_path: str = "tests"


class RepositoryAllowlistRequest(BaseModel):
    repository: str


def create_app(
    settings: AppSettings | None = None,
    service: GitHubAutomationService | None = None,
    store: SQLiteRunStore | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or AppSettings.from_env()
    service = service or GitHubAutomationService(settings)
    store = store or SQLiteRunStore(settings.resolved_database_path)
    repository_policy = RepositoryAllowlistPolicy(settings, store)
    worker = GitHubJobWorker(
        settings,
        service,
        store,
        repository_policy=repository_policy,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_webhook_settings(
            settings,
            require_docker=start_worker,
            repository_policy=repository_policy,
        )
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

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        llm_ready = settings.mock_llm or bool(os.getenv("DEEPSEEK_API_KEY"))
        return {
            "sandbox_backend": settings.sandbox_backend,
            "graph_backend": settings.graph_backend,
            "llm_mode": "mock" if settings.mock_llm else settings.llm_provider,
            "url_submission": settings.sandbox_backend == "docker" and llm_ready,
        }

    @app.get("/admin/repository-allowlist")
    def list_repository_allowlist(request: Request) -> dict[str, Any]:
        _require_admin(request, settings)
        return {
            "entries": [
                entry.model_dump(mode="json") for entry in repository_policy.list_entries()
            ]
        }

    @app.post("/admin/repository-allowlist")
    async def add_repository_allowlist(request: Request) -> dict[str, Any]:
        _require_admin(request, settings)
        payload = await _read_bounded_model(
            request,
            settings.webhook_max_bytes,
            RepositoryAllowlistRequest,
            "Invalid repository allowlist payload.",
        )
        try:
            entry = repository_policy.add(payload.repository)
        except RepositoryAllowlistLimitError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return entry.model_dump(mode="json")

    @app.delete("/admin/repository-allowlist/{owner}/{repository}")
    def remove_repository_allowlist(owner: str, repository: str, request: Request) -> dict[str, Any]:
        _require_admin(request, settings)
        try:
            repo_full_name = f"{owner}/{repository}"
            removed = repository_policy.remove(repo_full_name)
        except EnvironmentRepositoryRemovalError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {"repo_full_name": repo_full_name.lower(), "removed": removed}

    @app.get("/jobs")
    def list_jobs(request: Request, limit: int = 20) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, 100))
        return {
            "jobs": [
                _sanitize_payload(job.model_dump(mode="json", exclude={"payload_json"}))
                for job in store.list_github_jobs(bounded_limit)
            ]
        }

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        job = store.get_github_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return _sanitize_payload(job.model_dump(mode="json", exclude={"payload_json"}))

    @app.get("/jobs/{job_id}/events")
    def get_job_events(job_id: str, request: Request) -> dict[str, Any]:
        if store.get_github_job(job_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return {
            "events": [
                _sanitize_payload(event.model_dump(mode="json"))
                for event in store.list_job_events(job_id)
            ]
        }

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request):
        _require_admin(request, settings)
        try:
            job, accepted = store.request_github_job_cancellation(job_id)
        except JobTransitionError as exc:
            code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return _job_response(job, status.HTTP_202_ACCEPTED if accepted else status.HTTP_200_OK)

    @app.post("/jobs/{job_id}/retry")
    def retry_job(job_id: str, request: Request):
        _require_admin(request, settings)
        try:
            job = store.retry_github_job(job_id)
        except JobTransitionError as exc:
            code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return _job_response(job, status.HTTP_202_ACCEPTED)

    @app.post("/jobs/url", status_code=status.HTTP_202_ACCEPTED)
    async def submit_dashboard_url_job(request: Request):
        _require_admin(request, settings)
        try:
            content_length = int(request.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length.",
            ) from exc
        if content_length > settings.webhook_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large.")
        body = await request.body()
        if len(body) > settings.webhook_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large.")
        try:
            raw_payload = json.loads(body)
            if not isinstance(raw_payload, dict):
                raise ValueError("payload must be an object")
            payload = DashboardUrlJobRequest.model_validate(raw_payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid URL submission payload.",
            ) from exc
        try:
            repo_full_name = repository_policy.require_allowed(
                parse_github_repository_url(payload.repository_url)
            )
            target_path = (
                normalize_repo_path(payload.target_path)
                if payload.target_path and payload.target_path.strip()
                else AUTO_TARGET_PATH
            )
            if target_path != AUTO_TARGET_PATH and not target_path.lower().endswith(".py"):
                raise ValueError("Target path must reference a Python file.")
            tests_path = normalize_repo_path(payload.tests_path)
            branch = normalize_git_ref(payload.branch)
            issue_text = payload.refactor_request.strip()
            if not issue_text or len(issue_text) > 32768:
                raise ValueError("Refactor request must contain 1 to 32768 characters.")
        except RepositoryNotAllowlistedError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if settings.sandbox_backend != "docker" or (
            not settings.mock_llm and not os.getenv("DEEPSEEK_API_KEY")
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="URL submission requires Docker and an available configured LLM.",
            )
        job = GitHubRefactorJob(
            job_kind=RepositoryJobKind.DASHBOARD_URL,
            job_id=build_dashboard_job_id(repo_full_name),
            delivery_id=f"dashboard:{uuid4().hex}",
            repo_full_name=repo_full_name,
            default_branch=branch,
            issue_number=None,
            issue_title="Dashboard URL 本地简化任务",
            issue_text=issue_text,
            target_path=target_path,
            tests_path=tests_path,
            event_name="dashboard_url",
            action="submitted",
        )
        record = store.create_github_job(job)
        return _job_response(record, status.HTTP_202_ACCEPTED)

    @app.get("/runs")
    def list_runs(limit: int = 100) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, 100))
        return {
            "runs": [
                _sanitize_payload(record.model_dump(mode="json"))
                for record in store.list_runs(bounded_limit)
            ]
        }

    @app.get("/runs/{run_id}/trajectory")
    def get_run_trajectory(run_id: str) -> dict[str, Any]:
        if store.get(run_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        path = _safe_run_file(settings.run_root, run_id, "trajectory.jsonl")
        if not path.is_file():
            return {"trajectory": []}
        trajectory = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                trajectory.append(_sanitize_payload(json.loads(line)))
            except json.JSONDecodeError:
                trajectory.append({"status": "CORRUPT", "message": sanitize_text(line[:2048])})
        return {"trajectory": trajectory}

    @app.get("/runs/{run_id}/artifacts/{artifact_name}", response_class=PlainTextResponse)
    def get_run_artifact(run_id: str, artifact_name: str) -> PlainTextResponse:
        if store.get(run_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        try:
            path = resolve_artifact_path(settings.run_root, run_id, artifact_name)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found.") from exc
        if not path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found.")
        return PlainTextResponse(sanitize_text(path.read_text(encoding="utf-8")))

    @app.get("/benchmarks")
    def list_benchmarks(limit: int = 20) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, 100))
        return {
            "runs": [record.model_dump(mode="json") for record in store.list_benchmark_runs(bounded_limit)]
        }

    @app.get("/benchmarks/{run_id}")
    def get_benchmark(run_id: str) -> dict[str, Any]:
        run = store.get_benchmark_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark run not found.")
        return {
            "run": run.model_dump(mode="json"),
            "cases": [
                _sanitize_payload(record.model_dump(mode="json"))
                for record in store.list_benchmark_case_results(run_id)
            ],
        }

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

        allowed_senders = {item.lower() for item in settings.allowed_senders}
        if not repository_policy.is_allowed(parsed.repo_full_name):
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


def build_dashboard_job_id(repo_full_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo_full_name).strip("_") or "repo"
    return f"{safe_repo}__url__{stamp}-{uuid4().hex[:8]}"


def validate_webhook_settings(
    settings: AppSettings,
    require_docker: bool = False,
    repository_policy: RepositoryAllowlistPolicy | None = None,
) -> None:
    missing = []
    if not settings.github_webhook_secret:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not settings.admin_token:
        missing.append("REFACTOR_AGENT_ADMIN_TOKEN")
    if not (
        repository_policy.list_entries()
        if repository_policy is not None
        else settings.allowed_repositories
    ):
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


async def _read_bounded_model(
    request: Request,
    max_bytes: int,
    model_type: type[ModelT],
    invalid_detail: str,
) -> ModelT:
    try:
        content_length = int(request.headers.get("Content-Length", "0") or 0)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length.",
        ) from exc
    if content_length > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large.")
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large.")
    try:
        raw_payload = json.loads(body)
        if not isinstance(raw_payload, dict):
            raise ValueError("payload must be an object")
        return model_type.model_validate(raw_payload)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=invalid_detail) from exc


def _job_response(job, status_code: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content=_sanitize_payload(job.model_dump(mode="json", exclude={"payload_json"})),
    )


def _safe_run_file(run_root: Path, run_id: str, filename: str) -> Path:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    root = run_root.resolve()
    path = (root / run_id / filename).resolve()
    if not path.is_relative_to(root / run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return path


def _sanitize_payload(value):
    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items() if key != "payload_json"}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


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


def normalize_git_ref(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    ref = value.strip()
    invalid_tokens = ("..", "//", "@{")
    invalid_characters = set(" ~^:?*[\\")
    if (
        len(ref) > 200
        or ref.startswith(("-", "/", "."))
        or ref.endswith(("/", ".", ".lock"))
        or any(token in ref for token in invalid_tokens)
        or any(character in invalid_characters or ord(character) < 32 for character in ref)
    ):
        raise ValueError("Branch or tag is invalid.")
    return ref


def _join_text(*parts: str | None) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())
