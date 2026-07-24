from __future__ import annotations

from enum import StrEnum
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
    allowed_import_roots: set[str] = Field(default_factory=set)
    evidence_level: "EvidenceLevel" = Field(default_factory=lambda: EvidenceLevel.REPOSITORY_TESTS)
    persona: "ReportPersona" = Field(default_factory=lambda: ReportPersona.STRICT)


class AnalysisInputKind(StrEnum):
    SNIPPET = "SNIPPET"
    REPOSITORY_URL = "REPOSITORY_URL"


class EvidenceLevel(StrEnum):
    STATIC = "STATIC"
    GENERATED_TESTS = "GENERATED_TESTS"
    USER_TESTS = "USER_TESTS"
    REPOSITORY_TESTS = "REPOSITORY_TESTS"


class ReportPersona(StrEnum):
    STRICT = "STRICT"
    TSUNDERE = "TSUNDERE"


class AnalysisRequest(BaseModel):
    input_kind: AnalysisInputKind
    instruction: str = Field(min_length=1, max_length=32768)
    persona: ReportPersona = ReportPersona.STRICT
    source: str | None = None
    tests: str | None = None
    repository_url: str | None = None
    ref: str | None = None
    target_path: str | None = None
    tests_path: str | None = None


class AnalysisResult(BaseModel):
    task_id: str
    run_id: str | None = None
    status: str
    evidence_level: EvidenceLevel
    report_persona: ReportPersona
    product_mode: Literal["deepseek", "demo"]


class PersonaReport(BaseModel):
    persona: ReportPersona
    opening_verdict: str
    ast_assessment: str
    debate_summary: list[str] = Field(default_factory=list)
    metrics_assessment: str
    evidence_warning: str
    final_verdict: str
    commentary: str | None = None


class PersonaCopy(BaseModel):
    opening_verdict: str = Field(min_length=1, max_length=240)
    commentary: str = Field(min_length=1, max_length=1000)
    closing_verdict: str = Field(min_length=1, max_length=240)


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
    kind: Literal["function", "method", "module"] = "function"
    score: int = 0
    reason: str = "complexity fallback"


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
    selected_regions: list[TargetRegion] = Field(default_factory=list)
    allowed_regions: list[str] = Field(default_factory=list)
    changed_regions: list[str] = Field(default_factory=list)
    added_imports: list[str] = Field(default_factory=list)
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


class LLMUsage(BaseModel):
    provider: str
    model: str
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class LLMRefactorResult(BaseModel):
    thought: str
    fixed_code: str
    insult_review: str
    modified_regions: list[str] = Field(default_factory=list)
    usage: LLMUsage | None = None


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
        "CANCELLED",
        "TIMED_OUT",
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
    status: Literal["SUCCESS", "FAILED", "REVIEWED"]
    error: str | None = None
    evidence_level: EvidenceLevel = EvidenceLevel.REPOSITORY_TESTS
    report_persona: ReportPersona = ReportPersona.STRICT
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
    ast_rewrite: AstRewriteResult | None = None
    mutation_result: MutationTestResult | None = None
    performance_profile: PerformanceProfile | None = None
    adversarial_result: AdversarialTestResult | None = None
    debate_rounds: list[DebateRound] = Field(default_factory=list)
    graph_backend: str | None = None
    graph_node_trace: list[str] = Field(default_factory=list)
    llm_usages: list[LLMUsage] = Field(default_factory=list)
    evidence_level: EvidenceLevel = EvidenceLevel.REPOSITORY_TESTS
    report_persona: ReportPersona = ReportPersona.STRICT


class RepositoryJobKind(StrEnum):
    GITHUB_WEBHOOK = "GITHUB_WEBHOOK"
    DASHBOARD_URL = "DASHBOARD_URL"
    SNIPPET = "SNIPPET"


class GitHubRefactorJob(BaseModel):
    job_kind: RepositoryJobKind = RepositoryJobKind.GITHUB_WEBHOOK
    job_id: str
    delivery_id: str
    repo_full_name: str
    default_branch: str | None = "main"
    issue_number: int | None
    issue_title: str
    issue_text: str
    target_path: str
    tests_path: str = "tests"
    sender_login: str | None = None
    event_name: str
    action: str
    snippet_source: str | None = None
    snippet_tests: str | None = None
    snippet_mode: Literal["REVIEW", "VERIFIED_REFACTOR"] | None = None
    persona: Literal["STRICT", "TSUNDERE"] = "STRICT"


class GitHubAutomationResult(BaseModel):
    job_id: str | None = None
    repo_full_name: str
    issue_number: int | None
    branch_name: str | None = None
    run_id: str | None = None
    status: Literal["SUCCESS", "FAILED", "DRY_RUN"]
    pr_url: str | None = None
    workspace_path: Path | None = None
    error: str | None = None
    requires_manual_cleanup: bool = False


class GitHubJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DRY_RUN = "DRY_RUN"


class GitHubJobRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_id: str
    job_kind: RepositoryJobKind = RepositoryJobKind.GITHUB_WEBHOOK
    delivery_id: str
    repo_full_name: str
    issue_number: int | None
    target_path: str
    tests_path: str
    status: GitHubJobStatus
    branch_name: str | None = None
    run_id: str | None = None
    pr_url: str | None = None
    workspace_path: Path | None = None
    error: str | None = None
    payload_json: str | None = None
    attempt_count: int = 0
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    deadline_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class JobEventRecord(BaseModel):
    event_id: str
    job_id: str
    event_type: str
    from_status: GitHubJobStatus | None = None
    to_status: GitHubJobStatus | None = None
    worker_id: str | None = None
    attempt: int = 0
    message: str = ""
    created_at: str


class RepositoryAllowlistRecord(BaseModel):
    repo_full_name: str
    created_at: str


class RepositoryAllowlistEventRecord(BaseModel):
    event_id: str
    action: Literal["ADD", "REMOVE"]
    repo_full_name: str
    created_at: str


class RepositoryAllowlistEntry(BaseModel):
    repo_full_name: str
    source: Literal["ENVIRONMENT", "DASHBOARD"]
    removable: bool
    created_at: str | None = None


class BenchmarkRunRecord(BaseModel):
    run_id: str
    manifest_hash: str
    provider: str
    model: str
    status: Literal["SUCCESS", "FAILED"]
    generated_at: str


class BenchmarkCaseRecord(BaseModel):
    run_id: str
    case_name: str
    repository: str
    commit: str
    provider: str
    model: str
    status: str
    expected_status: str
    failure_category: str | None = None
    attempts: int = 0
    loc_before: int | None = None
    loc_after: int | None = None
    cc_before: int | None = None
    cc_after: int | None = None
    mutation_kill_rate: float | None = None
    adversarial_passed: bool | None = None
    runtime_seconds: float = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0
    normalized_hash: str
    error: str | None = None
