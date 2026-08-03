"""Prepare durable jobs from validated control API request contracts."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from refactor_agent.control_api_requests import DashboardUrlJobRequest, SnippetJobRequest
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.models import (
    AnalysisInputKind,
    AnalysisRequest,
    EvidenceLevel,
    GitHubRefactorJob,
    RepositoryJobKind,
)
from refactor_agent.repository_allowlist import parse_github_repository_url


class RepositoryAdmissionPolicy(Protocol):
    def require_allowed(self, repo_full_name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class PreparedAnalysisJob:
    job: GitHubRefactorJob
    evidence_level: EvidenceLevel


def prepare_dashboard_url_job(
    payload: DashboardUrlJobRequest,
    repository_policy: RepositoryAdmissionPolicy,
) -> GitHubRefactorJob:
    repo_full_name = repository_policy.require_allowed(
        parse_github_repository_url(payload.repository_url)
    )
    target_path = (
        normalize_repo_path(payload.target_path)
        if payload.target_path and payload.target_path.strip()
        else AUTO_TARGET_PATH
    )
    _require_python_target(target_path)
    tests_path = normalize_repo_path(payload.tests_path)
    branch = normalize_git_ref(payload.branch)
    instruction = _validated_instruction(payload.refactor_request)
    return GitHubRefactorJob(
        job_kind=RepositoryJobKind.DASHBOARD_URL,
        job_id=build_dashboard_job_id(repo_full_name),
        delivery_id=f"dashboard:{uuid4().hex}",
        repo_full_name=repo_full_name,
        default_branch=branch,
        issue_number=None,
        issue_title="Dashboard URL 本地简化任务",
        issue_text=instruction,
        target_path=target_path,
        tests_path=tests_path,
        event_name="dashboard_url",
        action="submitted",
        persona=payload.persona,
    )


def prepare_snippet_job(payload: SnippetJobRequest) -> GitHubRefactorJob:
    source, tests = _normalized_snippet(payload.source, payload.tests)
    _validate_snippet_sizes(source, tests)
    instruction = _validated_instruction(payload.refactor_request)
    if payload.mode == "VERIFIED_REFACTOR" and not tests:
        raise ValueError("Verified refactor mode requires pytest source.")
    _parse_snippet(source, tests)
    return _snippet_job(
        source=source,
        tests=tests,
        instruction=instruction,
        snippet_mode=payload.mode,
        persona=payload.persona,
        issue_title="Snippet code review",
        event_name="snippet",
    )


def prepare_analysis_job(
    payload: AnalysisRequest,
    repository_policy: RepositoryAdmissionPolicy,
) -> PreparedAnalysisJob:
    instruction = _validated_analysis_instruction(payload.instruction)
    if payload.input_kind == AnalysisInputKind.SNIPPET:
        source, tests = _normalized_snippet(payload.source or "", payload.tests)
        _validate_snippet_sizes(source, tests)
        _parse_snippet(source, tests)
        return PreparedAnalysisJob(
            job=_snippet_job(
                source=source,
                tests=tests,
                instruction=instruction,
                snippet_mode="VERIFIED_REFACTOR" if tests else "REVIEW",
                persona=payload.persona.value,
                issue_title="Snippet code analysis",
                event_name="analysis",
            ),
            evidence_level=EvidenceLevel.USER_TESTS if tests else EvidenceLevel.STATIC,
        )

    repository_url = payload.repository_url or ""
    repo_full_name = repository_policy.require_allowed(
        parse_github_repository_url(repository_url)
    )
    target_path = normalize_repo_path(payload.target_path) if payload.target_path else AUTO_TARGET_PATH
    _require_python_target(target_path)
    tests_path = normalize_repo_path(payload.tests_path or "tests")
    branch = normalize_git_ref(payload.ref)
    return PreparedAnalysisJob(
        job=GitHubRefactorJob(
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
        ),
        evidence_level=EvidenceLevel.REPOSITORY_TESTS,
    )


def build_dashboard_job_id(repo_full_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo_full_name).strip("_") or "repo"
    return f"{safe_repo}__url__{stamp}-{uuid4().hex[:8]}"


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


def _snippet_job(
    *,
    source: str,
    tests: str | None,
    instruction: str,
    snippet_mode: str,
    persona: str,
    issue_title: str,
    event_name: str,
) -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_kind=RepositoryJobKind.SNIPPET,
        job_id=f"snippet-{uuid4().hex}",
        delivery_id=f"snippet:{uuid4().hex}",
        repo_full_name="local/snippet",
        default_branch=None,
        issue_number=None,
        issue_title=issue_title,
        issue_text=instruction,
        target_path="snippet.py",
        tests_path="test_snippet.py",
        event_name=event_name,
        action="submitted",
        snippet_source=source + ("\n" if not source.endswith("\n") else ""),
        snippet_tests=(tests + ("\n" if not tests.endswith("\n") else "")) if tests else None,
        snippet_mode=snippet_mode,
        persona=persona,
    )


def _normalized_snippet(source_value: str, tests_value: str | None) -> tuple[str, str | None]:
    source = source_value.strip()
    tests = tests_value.strip() if tests_value else None
    return source, tests


def _validate_snippet_sizes(source: str, tests: str | None) -> None:
    if not source or len(source.encode("utf-8")) > 128 * 1024:
        raise ValueError("Source must contain 1 to 131072 UTF-8 bytes.")
    if tests and len(tests.encode("utf-8")) > 128 * 1024:
        raise ValueError("Tests must contain at most 131072 UTF-8 bytes.")


def _parse_snippet(source: str, tests: str | None) -> None:
    ast.parse(source, filename="snippet.py")
    if tests:
        ast.parse(tests, filename="test_snippet.py")


def _validated_instruction(value: str) -> str:
    instruction = value.strip()
    if not instruction or len(instruction) > 32768:
        raise ValueError("Refactor request must contain 1 to 32768 characters.")
    return instruction


def _validated_analysis_instruction(value: str) -> str:
    instruction = value.strip()
    if not instruction:
        raise ValueError("Analysis instruction must contain non-whitespace characters.")
    return instruction


def _require_python_target(target_path: str) -> None:
    if target_path != AUTO_TARGET_PATH and not target_path.lower().endswith(".py"):
        raise ValueError("Target path must reference a Python file.")
