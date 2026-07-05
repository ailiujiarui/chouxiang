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


class FunctionSignature(BaseModel):
    name: str
    qualified_name: str
    args: list[str]
    lineno: int
    end_lineno: int | None = None
    complexity: int


class ClassSummary(BaseModel):
    name: str
    lineno: int
    end_lineno: int | None = None
    methods: list[FunctionSignature] = Field(default_factory=list)


class SafetyFinding(BaseModel):
    rule: str
    message: str
    lineno: int | None = None
    severity: Literal["warning", "error"] = "error"


class AstAnalysis(BaseModel):
    loc: int
    cyclomatic_complexity: int
    functions: list[FunctionSignature] = Field(default_factory=list)
    classes: list[ClassSummary] = Field(default_factory=list)
    public_symbols: list[str] = Field(default_factory=list)
    high_complexity_regions: list[FunctionSignature] = Field(default_factory=list)
    safety_findings: list[SafetyFinding] = Field(default_factory=list)


class CandidateValidationResult(BaseModel):
    ok: bool
    analysis: AstAnalysis | None = None
    findings: list[SafetyFinding] = Field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "AST validation passed."
        return "\n".join(
            f"{finding.severity.upper()} {finding.rule}"
            f"{f' line {finding.lineno}' if finding.lineno else ''}: {finding.message}"
            for finding in self.findings
        )


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


class MutationTestResult(BaseModel):
    total: int
    killed: int
    survived: int
    survival_details: list[str] = Field(default_factory=list)

    @property
    def kill_rate(self) -> float:
        return self.killed / self.total if self.total else 1.0


class RewardBreakdown(BaseModel):
    delta_loc: int
    delta_cc: int
    retry_count: int
    mutation_kill_rate: float = 1.0
    reward: float


class TrajectoryStep(BaseModel):
    attempt: int
    status: Literal["AST_REJECTED", "PYTEST_FAILED", "SUCCESS", "FAILED"]
    message: str
    reward: RewardBreakdown | None = None


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
    ast_validation: CandidateValidationResult | None = None
    mutation_result: MutationTestResult | None = None


class GitHubRefactorJob(BaseModel):
    job_id: str
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
    job_id: str | None = None
    repo_full_name: str
    issue_number: int
    branch_name: str | None = None
    run_id: str | None = None
    status: Literal["SUCCESS", "FAILED", "DRY_RUN"]
    pr_url: str | None = None
    workspace_path: Path | None = None
    error: str | None = None


class GitHubJobRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_id: str
    repo_full_name: str
    issue_number: int
    target_path: str
    tests_path: str
    status: Literal["QUEUED", "RUNNING", "SUCCESS", "FAILED", "DRY_RUN"]
    branch_name: str | None = None
    run_id: str | None = None
    pr_url: str | None = None
    workspace_path: Path | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
