import logging
from pathlib import Path

from refactor_agent.errors import ErrorCode, public_error_message
from refactor_agent.models import RefactorRequest, TrajectoryMemoryRecord
from refactor_agent.orchestrator import _request_with_memory
from refactor_agent.orchestrator_prepare import prepare_execution_node, request_with_memory
from refactor_agent.orchestrator_state import initial_execution_state
from refactor_agent.sandbox import SandboxUnavailableError


class MemoryReader:
    def __init__(self, records: list[TrajectoryMemoryRecord]) -> None:
        self.records = records
        self.calls: list[tuple[str | None, str | None, int]] = []

    def list_memory(
        self,
        repo_name: str | None = None,
        target_path: str | None = None,
        limit: int = 5,
    ) -> list[TrajectoryMemoryRecord]:
        self.calls.append((repo_name, target_path, limit))
        return self.records


def test_prepare_execution_node_builds_isolated_state_with_memory(tmp_path: Path):
    request = _request(tmp_path)
    memory = TrajectoryMemoryRecord(
        memory_id="memory-1",
        run_id="old-run",
        repo_name="octo/demo",
        target_path="module.py",
        status="FAILED",
        lesson="Keep the public return value unchanged.",
    )
    store = MemoryReader([memory])
    state = initial_execution_state(2)
    workspace = tmp_path / "runs" / "run-1" / "workspace"

    returned = prepare_execution_node(
        state,
        request=request,
        workspace=workspace,
        store=store,
        repo_name="octo/demo",
        memory_key="module.py",
        sandbox_backend="subprocess",
    )

    assert returned is state
    assert state["next_node"] == "minimizer"
    assert state["active_backend"] == "subprocess"
    assert state["original_code"] == "def value():\n    return 1\n"
    assert state["current_code"] == state["original_code"]
    assert state["baseline"].loc == 2
    assert state["target_file"] == workspace / "module.py"
    assert state["tests_path"] == workspace / "tests"
    assert state["target_file"].read_text(encoding="utf-8") == state["original_code"]
    assert "Keep the public return value unchanged" in state["llm_request"].issue_text
    assert store.calls == [("octo/demo", "module.py", 3)]


def test_request_with_memory_preserves_request_and_compatibility_export(tmp_path: Path):
    request = _request(tmp_path)

    assert request_with_memory(request, None) is request
    updated = request_with_memory(request, "historical constraint")
    compatibility_updated = _request_with_memory(request, "historical constraint")

    assert updated is not request
    assert updated.issue_text.startswith(request.issue_text)
    assert updated.issue_text.endswith("historical constraint")
    assert updated.target_file == request.target_file
    assert updated.tests_path == request.tests_path
    assert compatibility_updated == updated


def test_prepare_execution_node_routes_sandbox_unavailable_to_finalize(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    request = _request(tmp_path)
    state = initial_execution_state(2)

    def unavailable(_backend: str):
        raise SandboxUnavailableError("virtualization missing")

    monkeypatch.setattr(
        "refactor_agent.orchestrator_prepare.resolve_sandbox_backend",
        unavailable,
    )
    with caplog.at_level(logging.WARNING):
        prepare_execution_node(
            state,
            request=request,
            workspace=tmp_path / "workspace",
            store=MemoryReader([]),
            repo_name="octo/demo",
            memory_key="module.py",
            sandbox_backend="docker",
        )

    assert state["next_node"] == "finalize"
    assert state["terminal_error"] == public_error_message(ErrorCode.INTERNAL_ERROR)
    assert state["terminal_error_code"] == ErrorCode.INTERNAL_ERROR
    assert state["terminal_error_summary"] == "sandbox backend unavailable"
    assert "Sandbox backend is unavailable: virtualization missing" in caplog.text


def _request(tmp_path: Path) -> RefactorRequest:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    target = project / "module.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    (tests / "test_module.py").write_text(
        "from module import value\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    return RefactorRequest(
        target_file=target,
        issue_text="Simplify value",
        tests_path=tests,
        repo_name="octo/demo",
        max_retry=2,
    )
