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


class TargetRegion(BaseModel):
    qualified_name: str
    lineno: int
    end_lineno: int
    complexity: int
    node_count: int
    structural_entropy: float


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
    target_regions: list[TargetRegion] = Field(default_factory=list)
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


class AstRewriteResult(BaseModel):
    ok: bool
    source: str
    allowed_regions: list[str] = Field(default_factory=list)
    changed_regions: list[str] = Field(default_factory=list)
    findings: list[SafetyFinding] = Field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            regions = ", ".join(self.changed_regions) or "none"
            return f"AST subtree rewrite passed; changed regions: {regions}."
        return "\n".join(
            f"{finding.severity.upper()} {finding.rule}"
            f"{f' line {finding.lineno}' if finding.lineno else ''}: {finding.message}"
            for finding in self.findings
        )


class LLMRefactorResult(BaseModel):
    thought: str
    fixed_code: str
    insult_review: str
    modified_regions: list[str] = Field(default_factory=list)


class LLMDefenderReviewResult(BaseModel):
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    boundary_risks: list[str] = Field(default_factory=list)
    readability_risks: list[str] = Field(default_factory=list)
    conservative_fix_suggestion: str


class LLMAdversaryTestResult(BaseModel):
    thought: str
    pytest_code: str = ""
    hypothesis_code: str = ""
    attack_plan: list[str] = Field(default_factory=list)


class LLMJudgeReviewResult(BaseModel):
    verdict: Literal["APPROVE", "RETRY", "REJECT"]
    rationale: str
    review: str


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


class AdversarialTestResult(BaseModel):
    generated: int
    passed: bool
    returncode: int
    test_file: Path | None = None
    stdout: str = ""
    stderr: str = ""


class AdversarialCritique(BaseModel):
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    attack_plan: list[str] = Field(default_factory=list)
    counterexample_hint: str | None = None
    rationale: str


class PerformanceProfile(BaseModel):
    passed: bool
    pytest_returncode: int
    pytest_duration_seconds: float
    peak_memory_kib: float
    import_time_seconds: float | None = None
    stdout: str = ""
    stderr: str = ""


class RewardBreakdown(BaseModel):
    delta_loc: int
    delta_cc: int
    retry_count: int
    mutation_kill_rate: float = 1.0
    adversarial_passed: bool = True
    reward: float


class AgentDebateMessage(BaseModel):
    round: int
    agent: Literal["MINIMIZER", "DEFENDER", "ADVERSARY", "JUDGE"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DebateRound(BaseModel):
    round: int
    candidate_loc: int | None = None
    candidate_cc: int | None = None
    pytest_passed: bool = False
    adversarial_passed: bool | None = None
    mutation_kill_rate: float | None = None
    reward: RewardBreakdown | None = None
    code_change_percent: float | None = None
    converged: bool = False
    messages: list[AgentDebateMessage] = Field(default_factory=list)


class TrajectoryStep(BaseModel):
    attempt: int
    status: Literal[
        "MINIMIZER_PROPOSED",
        "DEFENDER_REVIEWED",
        "AST_REJECTED",
        "PYTEST_FAILED",
        "ADVERSARY_CRITIQUED",
        "ADVERSARY_CHALLENGED",
        "ADVERSARY_FAILED",
        "JUDGE_SCORED",
        "DEBATE_CONVERGED",
        "SUCCESS",
        "FAILED",
    ]
    message: str
    agent: Literal["MINIMIZER", "DEFENDER", "ADVERSARY", "JUDGE", "SYSTEM"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    pytest_duration_seconds: float | None = None
    profiled_pytest_duration_seconds: float | None = None
    peak_memory_kib: float | None = None
    import_time_seconds: float | None = None


class TrajectoryMemoryRecord(BaseModel):
    memory_id: str
    run_id: str
    repo_name: str
    target_path: str
    status: Literal["SUCCESS", "FAILED"]
    lesson: str
    error_signature: str | None = None
    reward: float | None = None
    created_at: str | None = None


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
    performance_profile: PerformanceProfile | None = None
    adversarial_result: AdversarialTestResult | None = None
    debate_rounds: list[DebateRound] = Field(default_factory=list)


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
