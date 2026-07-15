from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import typer
import uvicorn
from rich.console import Console

from refactor_agent.arena_export import write_arena_report
from refactor_agent.benchmark import render_benchmark_markdown, run_benchmark, serialize_benchmark
from refactor_agent.config import AppSettings
from refactor_agent.ast_analyzer import analyze_ast, ast_hotspot_prompt, ast_prompt_summary
from refactor_agent.debate_state import render_mermaid_state_diagram
from refactor_agent.demo_cases import DEMO_CASE_NAMES, get_demo_case, materialize_demo_case
from refactor_agent.demo_suite import DEFAULT_DEMO_SUITE_CASES, DemoSuiteRun, render_demo_suite_report
from refactor_agent.github_url import GitHubUrlError, checkout_github_url
from refactor_agent.llm import DeepSeekClient, LLMError, MockRefactorClient
from refactor_agent.models import RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore
from refactor_agent.webhook import validate_webhook_settings

app = typer.Typer(help="Local closed-loop code refactoring agent.")
console = Console()


@app.command()
def run(
    target: Path = typer.Option(..., "--target", "-t", help="Python file to refactor."),
    issue: Path = typer.Option(..., "--issue", "-i", help="Markdown/text file containing the bug report."),
    tests: Path = typer.Option(..., "--tests", help="Pytest file or directory to run."),
    repo_name: str | None = typer.Option(None, "--repo-name", help="Optional name stored in SQLite reports."),
    max_retry: int = typer.Option(3, "--max-retry", min=1, help="Maximum total LLM attempts."),
    timeout: float = typer.Option(30.0, "--timeout", help="Pytest timeout in seconds."),
    sandbox_backend: str = typer.Option("subprocess", "--sandbox-backend", help="subprocess, docker, or auto."),
    sandbox_docker_image: str = typer.Option("refactor-agent-sandbox:py312", "--sandbox-docker-image", help="Docker image for docker sandbox backend."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated run workspaces."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    mock: bool = typer.Option(False, "--mock", help="Use deterministic local mock LLM."),
    mock_fail_times: int = typer.Option(0, "--mock-fail-times", min=0, help="Make mock LLM intentionally fail this many first attempts."),
    allow_import: list[str] | None = typer.Option(None, "--allow-import", help="Allow a new import root; repeat as needed."),
) -> None:
    """Run the refactor loop on a local target file."""
    _validate_input(target, issue, tests)
    request = RefactorRequest(
        target_file=target,
        issue_text=issue.read_text(encoding="utf-8"),
        tests_path=tests,
        repo_name=repo_name,
        max_retry=max_retry,
        allowed_import_roots=set(allow_import or []),
    )
    result = _run_request(request, run_root, database, timeout, mock, sandbox_backend, sandbox_docker_image, mock_fail_times)
    _print_plain(result.report_markdown)
    raise typer.Exit(code=0 if result.record.status == "SUCCESS" else 1)


@app.command()
def demo(
    case: str = typer.Option("leap-year", "--case", help=f"Demo case: {', '.join(DEMO_CASE_NAMES)}."),
    max_retry: int = typer.Option(3, "--max-retry", min=1, help="Maximum total LLM attempts."),
    timeout: float = typer.Option(30.0, "--timeout", help="Pytest timeout in seconds."),
    sandbox_backend: str = typer.Option("subprocess", "--sandbox-backend", help="subprocess, docker, or auto."),
    sandbox_docker_image: str = typer.Option("refactor-agent-sandbox:py312", "--sandbox-docker-image", help="Docker image for docker sandbox backend."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated run workspaces."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    real_api: bool = typer.Option(False, "--real-api", help="Use DeepSeek instead of the built-in mock."),
    mock_fail_times: int = typer.Option(0, "--mock-fail-times", min=0, help="Make mock LLM intentionally fail this many first attempts."),
) -> None:
    """Run a built-in live demo case. Uses mock LLM unless --real-api is set."""
    run_root = _resolve_run_root(run_root)
    try:
        target, issue, tests = materialize_demo_case(case, run_root)
        selected = get_demo_case(case)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    request = RefactorRequest(
        target_file=target,
        issue_text=issue.read_text(encoding="utf-8"),
        tests_path=tests,
        repo_name=f"demo-{selected.name}",
        max_retry=max_retry,
    )
    result = _run_request(
        request,
        run_root,
        database,
        timeout,
        mock=not real_api,
        sandbox_backend=sandbox_backend,
        sandbox_docker_image=sandbox_docker_image,
        mock_fail_times=mock_fail_times,
    )
    _print_plain(result.report_markdown)
    raise typer.Exit(code=0 if result.record.status == "SUCCESS" else 1)


@app.command("demo-suite")
def demo_suite(
    cases: list[str] | None = typer.Option(
        None,
        "--case",
        "-c",
        help="Demo case to run; repeat this option to customize the suite.",
    ),
    max_retry: int = typer.Option(3, "--max-retry", min=1, help="Maximum total LLM attempts per case."),
    timeout: float = typer.Option(30.0, "--timeout", help="Pytest timeout in seconds."),
    sandbox_backend: str = typer.Option("auto", "--sandbox-backend", help="subprocess, docker, or auto."),
    sandbox_docker_image: str = typer.Option(
        "refactor-agent-sandbox:py312",
        "--sandbox-docker-image",
        help="Docker image for docker sandbox backend.",
    ),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated run workspaces."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    real_api: bool = typer.Option(False, "--real-api", help="Use DeepSeek instead of the built-in mock."),
    mock_fail_times: int = typer.Option(
        0,
        "--mock-fail-times",
        min=0,
        help="Make mock LLM intentionally fail this many first attempts for every case.",
    ),
    dramatic_retry: bool = typer.Option(
        True,
        "--dramatic-retry/--no-dramatic-retry",
        help="In mock mode, force the adversarial weekend case to self-heal once for better live-demo contrast.",
    ),
    full_report: bool = typer.Option(False, "--full-report", help="Print every case report before the suite summary."),
) -> None:
    """Run the staged live-demo suite and print a Chinese battle report."""
    run_root = _resolve_run_root(run_root)
    database_path = _resolve_database(database, run_root)
    selected_cases = tuple(cases or DEFAULT_DEMO_SUITE_CASES)
    suite_runs: list[DemoSuiteRun] = []

    for case_name in selected_cases:
        try:
            target, issue, tests = materialize_demo_case(case_name, run_root)
            selected = get_demo_case(case_name)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc

        _print_plain(f"\n=== 路演案例: {selected.name} / {selected.title} ===")
        request = RefactorRequest(
            target_file=target,
            issue_text=issue.read_text(encoding="utf-8"),
            tests_path=tests,
            repo_name=f"demo-{selected.name}",
            max_retry=max_retry,
        )
        case_fail_times = _suite_mock_fail_times(case_name, mock_fail_times, real_api, dramatic_retry)
        result = _run_request(
            request,
            run_root,
            database_path,
            timeout,
            mock=not real_api,
            sandbox_backend=sandbox_backend,
            sandbox_docker_image=sandbox_docker_image,
            mock_fail_times=case_fail_times,
        )
        suite_runs.append(DemoSuiteRun(case_name=selected.name, title=selected.title, result=result))
        status_text = "成功" if result.record.status == "SUCCESS" else "失败"
        _print_plain(
            f"完成: {status_text} | 自愈 {result.record.self_heal_count} 轮 | "
            f"workspace={result.workspace_path}"
        )
        if full_report:
            _print_plain(result.report_markdown)

    _print_plain("")
    _print_plain(render_demo_suite_report(suite_runs, run_root, database_path))
    failed = [item for item in suite_runs if item.result.record.status != "SUCCESS"]
    raise typer.Exit(code=1 if failed else 0)


@app.command("demo-cases")
def demo_cases() -> None:
    """List the built-in live demo cases."""
    for name in DEMO_CASE_NAMES:
        case = get_demo_case(name)
        console.print(f"{case.name}: {case.title}", markup=False)


@app.command()
def benchmark(
    output_dir: Path = typer.Option(Path("benchmark-results"), "--output-dir", help="Evidence output directory."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated benchmark workspaces."),
    timeout: float = typer.Option(30.0, "--timeout", help="Pytest timeout in seconds."),
    sandbox_backend: str = typer.Option("subprocess", "--sandbox-backend", help="subprocess or docker."),
    graph_backend: str = typer.Option("langgraph", "--graph-backend", help="langgraph or loop."),
) -> None:
    """Run the deterministic six-case benchmark and emit JSON and Markdown."""
    run_root = _resolve_run_root(run_root)
    observations = run_benchmark(
        run_root=run_root,
        sandbox_backend=sandbox_backend,
        graph_backend=graph_backend,
        timeout_seconds=timeout,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "benchmark.json"
    markdown_path = output_dir / "benchmark.md"
    markdown = render_benchmark_markdown(observations)
    json_path.write_text(serialize_benchmark(observations) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    _print_plain(markdown)
    _print_plain(f"\nJSON: {json_path}\nMarkdown: {markdown_path}")


@app.command("state-machine")
def state_machine() -> None:
    """Print the multi-agent debate state machine as Mermaid."""
    console.print(render_mermaid_state_diagram(), markup=False)


@app.command("ast-hotspots")
def ast_hotspots(
    target: Path = typer.Option(..., "--target", "-t", help="Python file to analyze."),
    max_regions: int = typer.Option(3, "--max-regions", min=1, max=10, help="Maximum hotspot subtrees to show."),
) -> None:
    """Print AST semantic summary and high-complexity subtree snippets."""
    if not target.is_file():
        console.print(f"[red]target file does not exist: {target}[/red]")
        raise typer.Exit(code=2)
    source = target.read_text(encoding="utf-8")
    try:
        analysis = analyze_ast(source)
    except SyntaxError as exc:
        console.print(f"[red]syntax error at line {exc.lineno}: {exc.msg}[/red]")
        raise typer.Exit(code=2) from exc

    _print_plain("### AST 语义摘要")
    _print_plain(ast_prompt_summary(analysis))
    _print_plain("\n### AST 热点子树")
    _print_plain(ast_hotspot_prompt(source, max_regions=max_regions))


@app.command("github-url")
def github_url(
    repo_url: str = typer.Option(..., "--repo-url", help="GitHub HTTPS/SSH clone URL."),
    target: str = typer.Option(..., "--target", "-t", help="Target Python file path inside the repository."),
    issue: Path | None = typer.Option(None, "--issue", "-i", help="Markdown/text file containing the bug report."),
    issue_text: str | None = typer.Option(None, "--issue-text", help="Inline bug report text."),
    tests: str = typer.Option("tests", "--tests", help="Pytest file or directory path inside the repository."),
    branch: str | None = typer.Option(None, "--branch", help="Optional branch or tag to clone."),
    repo_name: str | None = typer.Option(None, "--repo-name", help="Optional name stored in SQLite reports."),
    max_retry: int = typer.Option(3, "--max-retry", min=1, help="Maximum total LLM attempts."),
    timeout: float = typer.Option(30.0, "--timeout", help="Pytest timeout in seconds."),
    sandbox_backend: str = typer.Option("subprocess", "--sandbox-backend", help="subprocess, docker, or auto."),
    sandbox_docker_image: str = typer.Option(
        "refactor-agent-sandbox:py312",
        "--sandbox-docker-image",
        help="Docker image for docker sandbox backend.",
    ),
    github_workspace_root: Path = typer.Option(
        Path(".github-url-workspaces"),
        "--github-workspace-root",
        help="Directory for cloned GitHub URL workspaces.",
    ),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated run workspaces."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    mock: bool = typer.Option(False, "--mock", help="Use deterministic local mock LLM."),
    mock_fail_times: int = typer.Option(
        0,
        "--mock-fail-times",
        min=0,
        help="Make mock LLM intentionally fail this many first attempts.",
    ),
) -> None:
    """Clone a GitHub URL locally and print the optimized sarcastic refactor report."""
    run_root = _resolve_run_root(run_root)
    github_workspace_root = _resolve_github_workspace_root(github_workspace_root)
    body = _resolve_issue_text(issue, issue_text)
    try:
        checkout = checkout_github_url(
            repo_url=repo_url,
            workspace_root=github_workspace_root,
            target_path=target,
            tests_path=tests,
            branch=branch,
        )
    except GitHubUrlError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    request = RefactorRequest(
        target_file=checkout.target_file,
        issue_text=body,
        tests_path=checkout.tests_path,
        repo_name=repo_name or checkout.repo_name,
        max_retry=max_retry,
    )
    result = _run_request(
        request,
        run_root,
        database,
        timeout,
        mock,
        sandbox_backend,
        sandbox_docker_image,
        mock_fail_times,
    )
    _print_plain(result.report_markdown)
    _print_plain(f"\n克隆仓库: {checkout.checkout_path}")
    if result.candidate_file is not None:
        _print_plain(f"优化候选文件: {result.candidate_file}")
    raise typer.Exit(code=0 if result.record.status == "SUCCESS" else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host for the FastAPI webhook server."),
    port: int = typer.Option(8000, "--port", help="Port for the FastAPI webhook server."),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn auto-reload."),
) -> None:
    """Serve the GitHub Webhook gateway."""
    settings = AppSettings.from_env()
    try:
        validate_webhook_settings(settings, require_docker=True)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    uvicorn.run("refactor_agent.webhook:app", host=host, port=port, reload=reload)


@app.command()
def jobs(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum number of jobs to show."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Run root used to infer the default database."),
) -> None:
    """List recent GitHub Webhook jobs."""
    run_root = _resolve_run_root(run_root)
    store = SQLiteRunStore(_resolve_database(database, run_root))
    records = store.list_github_jobs(limit)
    if not records:
        console.print("No GitHub jobs recorded yet.", markup=False)
        return
    for record in records:
        console.print(
            (
                f"{record.updated_at} | {record.status} | {record.job_id} | "
                f"{record.repo_full_name}#{record.issue_number} | "
                f"target={record.target_path} | run={record.run_id or '-'} | pr={record.pr_url or '-'}"
            ),
            markup=False,
        )


@app.command("memories")
def memories(
    repo_name: str | None = typer.Option(None, "--repo-name", help="Filter by stored repository name."),
    target: str | None = typer.Option(None, "--target", help="Filter by target file memory key, usually the filename."),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum number of memory records to show."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Run root used to infer the default database."),
) -> None:
    """List trajectory memory records learned from previous runs."""
    run_root = _resolve_run_root(run_root)
    store = SQLiteRunStore(_resolve_database(database, run_root))
    records = store.list_memory(repo_name=repo_name, target_path=target, limit=limit)
    if not records:
        _print_plain("还没有轨迹记忆。先跑一次 refactor-agent demo 或 github-url。")
        return
    for record in records:
        reward = f"{record.reward:.2f}" if record.reward is not None else "-"
        signature = record.error_signature or "-"
        _print_plain(
            (
                f"{record.created_at or '-'} | {record.status} | {record.repo_name} | "
                f"{record.target_path} | reward={reward} | error={signature}\n"
                f"  {record.lesson}"
            )
        )


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Host for the Streamlit arena."),
    port: int = typer.Option(8501, "--port", help="Port for the Streamlit arena."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Run root containing trajectories."),
) -> None:
    """Launch the live demo arena."""
    run_root = _resolve_run_root(run_root)
    database_path = _resolve_database(database, run_root)
    if importlib.util.find_spec("streamlit") is None:
        console.print("Streamlit is not installed. Install it with: python -m pip install -e .[dashboard]", markup=False)
        raise typer.Exit(code=1)

    script = Path(__file__).with_name("dashboard.py")
    env = os.environ.copy()
    env["REFACTOR_AGENT_DASHBOARD_DB"] = str(database_path)
    env["REFACTOR_AGENT_RUN_ROOT"] = str(run_root)
    console.print(f"Arena URL: http://{host}:{port}", markup=False)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.address",
            host,
            "--server.port",
            str(port),
        ],
        env=env,
    )
    raise typer.Exit(code=completed.returncode)


@app.command("arena-export")
def arena_export(
    output: Path = typer.Option(Path("arena-report.md"), "--output", "-o", help="Markdown report output path."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum number of recent runs to export."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Run root containing trajectories."),
) -> None:
    """Export recent arena runs to a static Markdown report."""
    run_root = _resolve_run_root(run_root)
    database_path = _resolve_database(database, run_root)
    path = write_arena_report(
        database_path=database_path,
        run_root=run_root,
        output_path=output,
        limit=limit,
    )
    _print_plain(f"竞技场战报已导出: {path}")


def _run_request(
    request: RefactorRequest,
    run_root: Path,
    database: Path | None,
    timeout: float,
    mock: bool,
    sandbox_backend: str = "subprocess",
    sandbox_docker_image: str = "refactor-agent-sandbox:py312",
    mock_fail_times: int = 0,
    graph_backend: str | None = None,
):
    run_root = _resolve_run_root(run_root)
    try:
        llm_client = MockRefactorClient(fail_times=mock_fail_times) if mock else DeepSeekClient()
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    store = SQLiteRunStore(_resolve_database(database, run_root))
    orchestrator = RefactorOrchestrator(
        llm_client=llm_client,
        run_root=run_root,
        store=store,
        pytest_timeout_seconds=timeout,
        sandbox_backend=sandbox_backend,
        sandbox_docker_image=sandbox_docker_image,
        graph_backend=graph_backend or os.getenv("REFACTOR_AGENT_GRAPH_BACKEND", "langgraph"),
    )
    return orchestrator.run(request)


def _suite_mock_fail_times(
    case_name: str,
    configured_fail_times: int,
    real_api: bool,
    dramatic_retry: bool,
) -> int:
    if real_api or not dramatic_retry:
        return configured_fail_times
    if case_name == "adversarial-weekend":
        return max(configured_fail_times, 1)
    return configured_fail_times


def _validate_input(target: Path, issue: Path, tests: Path) -> None:
    failures = []
    if not target.is_file():
        failures.append(f"target file does not exist: {target}")
    if not issue.is_file():
        failures.append(f"issue file does not exist: {issue}")
    if not tests.exists():
        failures.append(f"tests path does not exist: {tests}")
    if failures:
        for failure in failures:
            console.print(f"[red]{failure}[/red]")
        raise typer.Exit(code=2)


def _resolve_issue_text(issue: Path | None, issue_text: str | None) -> str:
    if issue_text and issue_text.strip():
        return issue_text
    if issue is None:
        console.print("[red]Provide --issue or --issue-text.[/red]")
        raise typer.Exit(code=2)
    if not issue.is_file():
        console.print(f"[red]issue file does not exist: {issue}[/red]")
        raise typer.Exit(code=2)
    return issue.read_text(encoding="utf-8")


def _resolve_run_root(run_root: Path) -> Path:
    env_run_root = os.getenv("REFACTOR_AGENT_RUN_ROOT")
    if env_run_root and run_root == Path(".runs"):
        return Path(env_run_root)
    return run_root


def _resolve_database(database: Path | None, run_root: Path) -> Path:
    if database is not None:
        return database
    env_database = os.getenv("REFACTOR_AGENT_DATABASE")
    if env_database:
        return Path(env_database)
    return run_root / "refactor_agent.sqlite"


def _resolve_github_workspace_root(github_workspace_root: Path) -> Path:
    env_workspace = os.getenv("REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT")
    if env_workspace and github_workspace_root == Path(".github-url-workspaces"):
        return Path(env_workspace)
    return github_workspace_root


def _print_plain(text: str) -> None:
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    console.print(safe_text, markup=False)


if __name__ == "__main__":
    app()
