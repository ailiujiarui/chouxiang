from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from refactor_agent.errors import ErrorCode, public_error_message
from refactor_agent.execution_graph import ExecutionState
from refactor_agent.memory import build_memory_context
from refactor_agent.metrics import analyze_file
from refactor_agent.models import RefactorRequest, TrajectoryMemoryRecord
from refactor_agent.orchestrator_state import transition_to
from refactor_agent.sandbox import (
    SandboxUnavailableError,
    prepare_workspace,
    resolve_sandbox_backend,
)


logger = logging.getLogger(__name__)


class TrajectoryMemoryReader(Protocol):
    def list_memory(
        self,
        repo_name: str | None = None,
        target_path: str | None = None,
        limit: int = 5,
    ) -> list[TrajectoryMemoryRecord]: ...


def prepare_execution_node(
    state: ExecutionState,
    *,
    request: RefactorRequest,
    workspace: Path,
    store: TrajectoryMemoryReader,
    repo_name: str,
    memory_key: str,
    sandbox_backend: str,
) -> ExecutionState:
    """Prepare one isolated workspace and select its executable sandbox backend."""
    memory = build_memory_context(store.list_memory(repo_name, memory_key, limit=3))
    state["llm_request"] = request_with_memory(request, memory)
    state["baseline"] = analyze_file(request.target_file)
    state["original_code"] = request.target_file.read_text(encoding="utf-8")
    state["current_code"] = state["original_code"]
    _, state["target_file"], state["tests_path"] = prepare_workspace(
        request.target_file,
        request.tests_path,
        workspace,
    )
    try:
        state["active_backend"], _ = resolve_sandbox_backend(sandbox_backend)
    except SandboxUnavailableError as exc:
        logger.warning("Sandbox backend is unavailable: %s", exc)
        state["terminal_error"] = public_error_message(ErrorCode.INTERNAL_ERROR)
        state["terminal_error_code"] = ErrorCode.INTERNAL_ERROR
        state["terminal_error_summary"] = "sandbox backend unavailable"
        return transition_to(state, "finalize")
    return transition_to(state, "minimizer")


def request_with_memory(
    request: RefactorRequest,
    memory_context: str | None,
) -> RefactorRequest:
    if not memory_context:
        return request
    return request.model_copy(
        update={
            "issue_text": (
                f"{request.issue_text}\n\n"
                "以下是系统从历史轨迹中提炼出的短期记忆，请把它当作额外约束：\n"
                f"{memory_context}"
            )
        }
    )
