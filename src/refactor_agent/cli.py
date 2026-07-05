from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from refactor_agent.config import AppSettings
from refactor_agent.llm import DeepSeekClient, LLMError, MockRefactorClient
from refactor_agent.models import RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore

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
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated run workspaces."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    mock: bool = typer.Option(False, "--mock", help="Use deterministic local mock LLM."),
) -> None:
    """Run the refactor loop on a local target file."""
    _validate_input(target, issue, tests)
    request = RefactorRequest(
        target_file=target,
        issue_text=issue.read_text(encoding="utf-8"),
        tests_path=tests,
        repo_name=repo_name,
        max_retry=max_retry,
    )
    result = _run_request(request, run_root, database, timeout, mock)
    console.print(result.report_markdown, markup=False)
    raise typer.Exit(code=0 if result.record.status == "SUCCESS" else 1)


@app.command()
def demo(
    max_retry: int = typer.Option(3, "--max-retry", min=1, help="Maximum total LLM attempts."),
    timeout: float = typer.Option(30.0, "--timeout", help="Pytest timeout in seconds."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Directory for isolated run workspaces."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    real_api: bool = typer.Option(False, "--real-api", help="Use DeepSeek instead of the built-in mock."),
) -> None:
    """Run the built-in leap-year demo. Uses mock LLM unless --real-api is set."""
    demo_root = files("refactor_agent").joinpath("demo_project")
    target = Path(str(demo_root.joinpath("leap_year.py")))
    issue = Path(str(demo_root.joinpath("issue.md")))
    tests = Path(str(demo_root.joinpath("tests")))
    request = RefactorRequest(
        target_file=target,
        issue_text=issue.read_text(encoding="utf-8"),
        tests_path=tests,
        repo_name="demo-leap-year",
        max_retry=max_retry,
    )
    result = _run_request(request, run_root, database, timeout, mock=not real_api)
    console.print(result.report_markdown, markup=False)
    raise typer.Exit(code=0 if result.record.status == "SUCCESS" else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host for the FastAPI webhook server."),
    port: int = typer.Option(8000, "--port", help="Port for the FastAPI webhook server."),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn auto-reload."),
) -> None:
    """Serve the GitHub Webhook gateway."""
    uvicorn.run("refactor_agent.webhook:app", host=host, port=port, reload=reload)


@app.command()
def jobs(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum number of jobs to show."),
    database: Path | None = typer.Option(None, "--database", help="SQLite database path."),
    run_root: Path = typer.Option(Path(".runs"), "--run-root", help="Run root used to infer the default database."),
) -> None:
    """List recent GitHub Webhook jobs."""
    settings = AppSettings(run_root=run_root, database_path=database)
    store = SQLiteRunStore(settings.resolved_database_path)
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


def _run_request(
    request: RefactorRequest,
    run_root: Path,
    database: Path | None,
    timeout: float,
    mock: bool,
):
    try:
        llm_client = MockRefactorClient() if mock else DeepSeekClient()
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    store = SQLiteRunStore(database or (run_root / "refactor_agent.sqlite"))
    orchestrator = RefactorOrchestrator(
        llm_client=llm_client,
        run_root=run_root,
        store=store,
        pytest_timeout_seconds=timeout,
    )
    return orchestrator.run(request)


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
