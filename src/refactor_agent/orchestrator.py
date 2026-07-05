from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.llm import LLMError, RefactorClient
from refactor_agent.metrics import analyze_file
from refactor_agent.models import RefactorRequest, RefactorRunResult, RunRecord, SandboxResult
from refactor_agent.sandbox import prepare_workspace, run_pytest, write_candidate
from refactor_agent.store import SQLiteRunStore


class RefactorOrchestrator:
    def __init__(
        self,
        llm_client: RefactorClient,
        run_root: Path = Path(".runs"),
        store: SQLiteRunStore | None = None,
        pytest_timeout_seconds: float = 30.0,
    ) -> None:
        self.llm_client = llm_client
        self.run_root = run_root.resolve()
        self.store = store or SQLiteRunStore(self.run_root / "refactor_agent.sqlite")
        self.pytest_timeout_seconds = pytest_timeout_seconds

    def run(self, request: RefactorRequest) -> RefactorRunResult:
        run_id = _new_run_id()
        workspace = self.run_root / run_id / "workspace"
        repo_name = request.repo_name or request.target_file.resolve().parent.name
        baseline = analyze_file(request.target_file)
        original_code = request.target_file.read_text(encoding="utf-8")
        _, target_in_workspace, tests_in_workspace = prepare_workspace(
            request.target_file,
            request.tests_path,
            workspace,
        )

        current_code = original_code
        previous_error: str | None = None
        last_sandbox: SandboxResult | None = None

        for attempt in range(1, request.max_retry + 1):
            try:
                llm_result = self.llm_client.refactor(
                    request=request,
                    current_code=current_code,
                    baseline_metrics=baseline,
                    previous_error=previous_error,
                    attempt=attempt,
                )
            except LLMError as exc:
                record = RunRecord(
                    run_id=run_id,
                    issue_id=request.issue_id,
                    repo_name=repo_name,
                    pre_loc=baseline.loc,
                    pre_cc=baseline.cyclomatic_complexity,
                    self_heal_count=attempt - 1,
                    status="FAILED",
                    error=str(exc),
                )
                self.store.save(record)
                return RefactorRunResult(
                    record=record,
                    report_markdown=_build_report(record, workspace, None, None, str(exc)),
                    workspace_path=workspace,
                    attempts=attempt,
                    last_sandbox_result=last_sandbox,
                    candidate_file=target_in_workspace,
                )

            current_code = llm_result.fixed_code
            write_candidate(target_in_workspace, current_code)
            last_sandbox = run_pytest(
                workspace=workspace,
                tests_path=tests_in_workspace,
                timeout_seconds=self.pytest_timeout_seconds,
            )
            if last_sandbox.passed:
                post = analyze_file(target_in_workspace)
                record = RunRecord(
                    run_id=run_id,
                    issue_id=request.issue_id,
                    repo_name=repo_name,
                    pre_loc=baseline.loc,
                    post_loc=post.loc,
                    pre_cc=baseline.cyclomatic_complexity,
                    post_cc=post.cyclomatic_complexity,
                    self_heal_count=attempt - 1,
                    status="SUCCESS",
                )
                self.store.save(record)
                return RefactorRunResult(
                    record=record,
                    report_markdown=_build_report(record, workspace, llm_result.insult_review, last_sandbox, None),
                    workspace_path=workspace,
                    attempts=attempt,
                    last_sandbox_result=last_sandbox,
                    candidate_file=target_in_workspace,
                )

            previous_error = _summarize_failure(last_sandbox)

        record = RunRecord(
            run_id=run_id,
            issue_id=request.issue_id,
            repo_name=repo_name,
            pre_loc=baseline.loc,
            pre_cc=baseline.cyclomatic_complexity,
            self_heal_count=request.max_retry,
            status="FAILED",
            error=previous_error or "pytest failed",
        )
        self.store.save(record)
        return RefactorRunResult(
            record=record,
            report_markdown=_build_report(record, workspace, None, last_sandbox, record.error),
            workspace_path=workspace,
            attempts=request.max_retry,
            last_sandbox_result=last_sandbox,
            candidate_file=target_in_workspace,
        )


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"


def _summarize_failure(result: SandboxResult) -> str:
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    return combined[-8000:] if combined else f"pytest failed with return code {result.returncode}"


def _build_report(
    record: RunRecord,
    workspace: Path,
    review: str | None,
    sandbox_result: SandboxResult | None,
    error: str | None,
) -> str:
    loc_delta = _delta(record.pre_loc, record.post_loc)
    cc_delta = _delta(record.pre_cc, record.post_cc)
    lines = [
        "### Refactor Agent Report",
        "",
        f"- Status: **{record.status}**",
        f"- Run ID: `{record.run_id}`",
        f"- Workspace: `{workspace}`",
        f"- Self-heal count: {record.self_heal_count}",
        f"- LOC: {record.pre_loc} -> {record.post_loc} ({loc_delta})",
        f"- Cyclomatic Complexity: {record.pre_cc} -> {record.post_cc} ({cc_delta})",
    ]
    if sandbox_result is not None:
        lines.extend(
            [
                f"- Pytest return code: {sandbox_result.returncode}",
                f"- Pytest duration: {sandbox_result.duration_seconds:.2f}s",
            ]
        )
    if review:
        lines.extend(["", "#### Code Review", "", review])
    if error:
        lines.extend(["", "#### Error", "", "```text", error[-4000:], "```"])
    return "\n".join(lines)


def _delta(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    change = after - before
    if before == 0:
        return f"{change:+d}"
    percentage = (change / before) * 100
    return f"{change:+d}, {percentage:+.1f}%"
