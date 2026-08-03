from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from refactor_agent.execution_control import ExecutionControl
from refactor_agent.llm import DeepSeekClient, LLMError, MockRefactorClient
from refactor_agent.models import RefactorRequest, RefactorRunResult
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


class LocalRefactorConfigurationError(LLMError):
    """Raised when the selected local LLM client cannot be initialized."""


def run_local_refactor(
    request: RefactorRequest,
    *,
    run_root: Path,
    database_path: Path,
    pytest_timeout_seconds: float,
    mock: bool,
    sandbox_backend: str = "subprocess",
    sandbox_docker_image: str = "refactor-agent-sandbox:py312",
    mock_fail_times: int = 0,
    graph_backend: str = "langgraph",
    deadline_seconds: int = 900,
) -> RefactorRunResult:
    """Execute one local refactor without depending on CLI presentation or parsing."""

    try:
        llm_client = MockRefactorClient(fail_times=mock_fail_times) if mock else DeepSeekClient()
    except LLMError as exc:
        raise LocalRefactorConfigurationError(str(exc), code=exc.code) from exc
    orchestrator = RefactorOrchestrator(
        llm_client=llm_client,
        run_root=run_root,
        store=SQLiteRunStore(database_path),
        pytest_timeout_seconds=pytest_timeout_seconds,
        sandbox_backend=sandbox_backend,
        sandbox_docker_image=sandbox_docker_image,
        graph_backend=graph_backend,
    )
    control = ExecutionControl(
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
    )
    return orchestrator.run(request, execution_control=control)
