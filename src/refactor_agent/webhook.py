from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from refactor_agent.artifacts import resolve_artifact_path, sanitize_text
from refactor_agent.config import AppSettings
from refactor_agent.job_worker import GitHubJobWorker
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.models import (
    AnalysisInputKind,
    AnalysisRequest,
    EvidenceLevel,
    GitHubRefactorJob,
    RepositoryJobKind,
)
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
    persona: Literal["STRICT", "TSUNDERE"] = "STRICT"


class RepositoryAllowlistRequest(BaseModel):
    repository: str


class SnippetJobRequest(BaseModel):
    source: str
    refactor_request: str
    tests: str | None = None
    mode: Literal["REVIEW", "VERIFIED_REFACTOR"] = "REVIEW"
    persona: Literal["STRICT", "TSUNDERE"] = "STRICT"


def create_app(
    settings: AppSettings | None = None,
    store: SQLiteRunStore | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or AppSettings.from_env()
    store = store or SQLiteRunStore(settings.resolved_database_path)
    repository_policy = RepositoryAllowlistPolicy(settings, store)
    worker = GitHubJobWorker(
        settings,
        store,
        repository_policy=repository_policy,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_control_api_settings(
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

    app = FastAPI(title="Refactor Agent Control API", version="0.1.0", lifespan=lifespan)
    app.state.worker = worker

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/analysis/events/cursor")
    def get_analysis_event_cursor() -> dict[str, int]:
        return {"latest_sequence": store.latest_analysis_event_sequence()}

    @app.get("/analysis/events")
    def list_analysis_events(after: int = 0, limit: int = 100) -> dict[str, Any]:
        if after < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="after must be non-negative")
        events, next_sequence, latest_sequence, has_more = store.read_public_analysis_event_page(
            after=after,
            limit=limit,
        )
        return {
            "events": [_sanitize_payload(event.model_dump(mode="json")) for event in events],
            "next_sequence": next_sequence,
            "latest_sequence": latest_sequence,
            "has_more": has_more,
        }

    @app.get("/analysis/events/stream")
    async def stream_analysis_events(request: Request, after: int = 0) -> StreamingResponse:
        if after < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="after must be non-negative")
        header_cursor = request.headers.get("Last-Event-ID")
        if header_cursor:
            try:
                after = max(after, int(header_cursor))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be an integer",
                ) from exc

        async def event_stream():
            cursor = after
            last_heartbeat = time.monotonic()
            yield "retry: 1000\n\n"
            while not await request.is_disconnected():
                events = store.list_analysis_events(after=cursor, limit=100)
                delivered = False
                for event in events:
                    cursor = int(event.sequence or cursor)
                    if event.sensitivity != "public":
                        continue
                    payload = json.dumps(
                        _sanitize_payload(event.model_dump(mode="json")),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {cursor}\nevent: analysis_event\ndata: {payload}\n\n"
                    delivered = True
                now = time.monotonic()
                if not delivered and now - last_heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        llm_ready = settings.mock_llm or bool(os.getenv("DEEPSEEK_API_KEY"))
        product_mode = "demo" if settings.mock_llm else "deepseek"
        return {
            "sandbox_backend": settings.sandbox_backend,
            "graph_backend": settings.graph_backend,
            "llm_mode": "mock" if settings.mock_llm else settings.llm_provider,
            "product_mode": product_mode,
            "demo_limitations": (
                "Deterministic demo supports only built-in patterns; arbitrary code requires DeepSeek."
                if product_mode == "demo"
                else None
            ),
            "url_submission": settings.sandbox_backend == "docker" and llm_ready,
            "snippet_submission": True,
            "snippet_verified_refactor": settings.sandbox_backend == "docker" and llm_ready,
            "snippet_modes": ["REVIEW", "VERIFIED_REFACTOR"],
            "personas": ["STRICT", "TSUNDERE"],
            "admin_token_required": bool(settings.admin_token),
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
            settings.request_max_bytes,
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
        existing = store.get_github_job(job_id)
        if existing is not None and existing.job_kind == RepositoryJobKind.GITHUB_WEBHOOK:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GitHub Webhook delivery has been removed; legacy jobs cannot retry.",
            )
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
        if content_length > settings.request_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large.")
        body = await request.body()
        if len(body) > settings.request_max_bytes:
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
            persona=payload.persona,
        )
        record = store.create_github_job(job)
        return _job_response(record, status.HTTP_202_ACCEPTED)

    @app.post("/jobs/snippet", status_code=status.HTTP_202_ACCEPTED)
    async def submit_snippet_job(request: Request):
        _require_admin(request, settings)
        payload = await _read_bounded_model(
            request,
            settings.request_max_bytes,
            SnippetJobRequest,
            "Invalid snippet submission payload.",
        )
        source = payload.source.strip()
        tests = payload.tests.strip() if payload.tests else None
        requirement = payload.refactor_request.strip()
        try:
            if not source or len(source.encode("utf-8")) > 128 * 1024:
                raise ValueError("Source must contain 1 to 131072 UTF-8 bytes.")
            if tests and len(tests.encode("utf-8")) > 128 * 1024:
                raise ValueError("Tests must contain at most 131072 UTF-8 bytes.")
            if not requirement or len(requirement) > 32768:
                raise ValueError("Refactor request must contain 1 to 32768 characters.")
            if payload.mode == "VERIFIED_REFACTOR" and not tests:
                raise ValueError("Verified refactor mode requires pytest source.")
            ast.parse(source, filename="snippet.py")
            if tests:
                ast.parse(tests, filename="test_snippet.py")
        except (SyntaxError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if payload.mode == "VERIFIED_REFACTOR" and (
            settings.sandbox_backend != "docker"
            or (not settings.mock_llm and not os.getenv("DEEPSEEK_API_KEY"))
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Snippet submission requires Docker and an available configured LLM.",
            )
        job_id = f"snippet-{uuid4().hex}"
        job = GitHubRefactorJob(
            job_kind=RepositoryJobKind.SNIPPET,
            job_id=job_id,
            delivery_id=f"snippet:{uuid4().hex}",
            repo_full_name="local/snippet",
            default_branch=None,
            issue_number=None,
            issue_title="Snippet code review",
            issue_text=requirement,
            target_path="snippet.py",
            tests_path="test_snippet.py",
            event_name="snippet",
            action="submitted",
            snippet_source=source + ("\n" if not source.endswith("\n") else ""),
            snippet_tests=(tests + ("\n" if not tests.endswith("\n") else "")) if tests else None,
            snippet_mode=payload.mode,
            persona=payload.persona,
        )
        record = store.create_github_job(job)
        return _job_response(record, status.HTTP_202_ACCEPTED)

    @app.post("/analysis", status_code=status.HTTP_202_ACCEPTED)
    async def submit_analysis(request: Request):
        """Unified product entry point; legacy job routes remain compatibility adapters."""
        _require_admin(request, settings)
        payload = await _read_bounded_model(
            request,
            settings.request_max_bytes,
            AnalysisRequest,
            "Invalid analysis payload.",
        )
        instruction = payload.instruction.strip()
        if not instruction:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Analysis instruction must contain non-whitespace characters.",
            )
        if payload.input_kind == AnalysisInputKind.SNIPPET:
            source = (payload.source or "").strip()
            tests = payload.tests.strip() if payload.tests else None
            try:
                if not source or len(source.encode("utf-8")) > 128 * 1024:
                    raise ValueError("Source must contain 1 to 131072 UTF-8 bytes.")
                if tests and len(tests.encode("utf-8")) > 128 * 1024:
                    raise ValueError("Tests must contain at most 131072 UTF-8 bytes.")
                ast.parse(source, filename="snippet.py")
                if tests:
                    ast.parse(tests, filename="test_snippet.py")
            except (SyntaxError, ValueError) as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            job = GitHubRefactorJob(
                job_kind=RepositoryJobKind.SNIPPET,
                job_id=f"snippet-{uuid4().hex}",
                delivery_id=f"snippet:{uuid4().hex}",
                repo_full_name="local/snippet",
                issue_number=None,
                issue_title="Snippet code analysis",
                issue_text=instruction,
                target_path="snippet.py",
                tests_path="test_snippet.py",
                event_name="analysis",
                action="submitted",
                snippet_source=source + ("\n" if not source.endswith("\n") else ""),
                snippet_tests=(tests + ("\n" if not tests.endswith("\n") else "")) if tests else None,
                snippet_mode="VERIFIED_REFACTOR" if tests else "REVIEW",
                persona=payload.persona,
            )
            evidence = EvidenceLevel.USER_TESTS if tests else EvidenceLevel.STATIC
        else:
            try:
                repository_url = payload.repository_url or ""
                repo_full_name = repository_policy.require_allowed(
                    parse_github_repository_url(repository_url)
                )
                target_path = normalize_repo_path(payload.target_path) if payload.target_path else AUTO_TARGET_PATH
                if target_path != AUTO_TARGET_PATH and not target_path.lower().endswith(".py"):
                    raise ValueError("Target path must reference a Python file.")
                tests_path = normalize_repo_path(payload.tests_path or "tests")
                branch = normalize_git_ref(payload.ref)
            except RepositoryNotAllowlistedError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            if settings.sandbox_backend != "docker" or (
                not settings.mock_llm and not os.getenv("DEEPSEEK_API_KEY")
            ):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Repository analysis requires Docker and an available configured LLM.",
                )
            job = GitHubRefactorJob(
                job_kind=RepositoryJobKind.DASHBOARD_URL,
                job_id=build_dashboard_job_id(repo_full_name),
                delivery_id=f"dashboard:{uuid4().hex}",
                repo_full_name=repo_full_name,
                default_branch=branch,
                issue_number=None,
                issue_title="Repository code analysis",
                issue_text=instruction,
                target_path=target_path,
                tests_path=tests_path,
                event_name="analysis",
                action="submitted",
                persona=payload.persona,
            )
            evidence = EvidenceLevel.REPOSITORY_TESTS
        record = store.create_github_job(job)
        response_payload = _sanitize_payload(record.model_dump(mode="json", exclude={"payload_json"}))
        response_payload.update(
            evidence_level=evidence.value,
            report_persona=payload.persona.value,
            product_mode="demo" if settings.mock_llm else "deepseek",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=response_payload)

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

    return app


app = create_app()


def build_dashboard_job_id(repo_full_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo_full_name).strip("_") or "repo"
    return f"{safe_repo}__url__{stamp}-{uuid4().hex[:8]}"


def validate_control_api_settings(
    settings: AppSettings,
    require_docker: bool = False,
    repository_policy: RepositoryAllowlistPolicy | None = None,
) -> None:
    missing = []
    if not (
        repository_policy.list_entries()
        if repository_policy is not None
        else settings.allowed_repositories
    ):
        missing.append("REFACTOR_AGENT_ALLOWED_REPOSITORIES")
    if not settings.mock_llm and not os.getenv("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        raise RuntimeError("Control API configuration is fail-closed; missing: " + ", ".join(missing))
    if settings.sandbox_backend != "docker":
        raise RuntimeError("Control API requires REFACTOR_AGENT_SANDBOX_BACKEND=docker.")
    if require_docker:
        docker = docker_status()
        if not docker.available:
            raise RuntimeError(f"Control API requires an available Docker daemon: {docker.error}")


def _require_admin(request: Request, settings: AppSettings) -> None:
    if not settings.admin_token:
        return
    expected = f"Bearer {settings.admin_token or ''}"
    provided = request.headers.get("Authorization", "")
    if not secrets.compare_digest(provided, expected):
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
