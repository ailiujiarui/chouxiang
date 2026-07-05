from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RefactorRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    target_file: Path
    issue_text: str
    tests_path: Path
    repo_name: str | None = None
    issue_id: str | None = None
    max_retry: int = Field(default=3, ge=1)


class MetricsSnapshot(BaseModel):
    loc: int
    cyclomatic_complexity: int
    details: list[dict[str, Any]] = Field(default_factory=list)


class LLMRefactorResult(BaseModel):
    thought: str
    fixed_code: str
    insult_review: str


class SandboxResult(BaseModel):
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


class RunRecord(BaseModel):
    run_id: str
    issue_id: str | None = None
    repo_name: str
    pre_loc: int | None = None
    post_loc: int | None = None
    pre_cc: int | None = None
    post_cc: int | None = None
    self_heal_count: int
    status: Literal["SUCCESS", "FAILED"]
    error: str | None = None


class RefactorRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    record: RunRecord
    report_markdown: str
    workspace_path: Path
    attempts: int
    last_sandbox_result: SandboxResult | None = None
    candidate_file: Path | None = None


class GitHubRefactorJob(BaseModel):
    repo_full_name: str
    clone_url: str
    default_branch: str = "main"
    issue_number: int
    issue_title: str
    issue_text: str
    target_path: str
    tests_path: str = "tests"
    sender_login: str | None = None
    event_name: str
    action: str


class GitHubAutomationResult(BaseModel):
    repo_full_name: str
    issue_number: int
    branch_name: str | None = None
    run_id: str | None = None
    status: Literal["SUCCESS", "FAILED", "DRY_RUN"]
    pr_url: str | None = None
    workspace_path: Path | None = None
    error: str | None = None
