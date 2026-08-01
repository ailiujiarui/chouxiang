from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import refactor_agent.local_refactor as local_refactor
from refactor_agent.errors import ErrorCode
from refactor_agent.llm import LLMError, MockRefactorClient
from refactor_agent.local_refactor import LocalRefactorConfigurationError
from refactor_agent.models import RefactorRequest


def test_local_refactor_assembles_mock_execution_without_cli_dependencies(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    expected_result = object()
    store = object()
    fixed_now = datetime(2026, 8, 2, 1, 2, 3, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    class CapturingOrchestrator:
        def __init__(self, **kwargs) -> None:
            captured["orchestrator"] = kwargs

        def run(self, request, execution_control):
            captured["request"] = request
            captured["control"] = execution_control
            return expected_result

    def capture_store(path: Path):
        captured["database_path"] = path
        return store

    monkeypatch.setattr(local_refactor, "datetime", FixedDateTime)
    monkeypatch.setattr(local_refactor, "SQLiteRunStore", capture_store)
    monkeypatch.setattr(local_refactor, "RefactorOrchestrator", CapturingOrchestrator)
    request = _request(tmp_path)

    result = local_refactor.run_local_refactor(
        request,
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        pytest_timeout_seconds=12.5,
        mock=True,
        sandbox_backend="docker",
        sandbox_docker_image="sandbox:test",
        mock_fail_times=2,
        graph_backend="loop",
        deadline_seconds=321,
    )

    assembled = captured["orchestrator"]
    assert result is expected_result
    assert isinstance(assembled["llm_client"], MockRefactorClient)
    assert assembled["llm_client"].fail_times == 2
    assert assembled == {
        "llm_client": assembled["llm_client"],
        "run_root": tmp_path / "runs",
        "store": store,
        "pytest_timeout_seconds": 12.5,
        "sandbox_backend": "docker",
        "sandbox_docker_image": "sandbox:test",
        "graph_backend": "loop",
    }
    assert captured["database_path"] == tmp_path / "runs.sqlite"
    assert captured["request"] is request
    assert captured["control"].deadline_at == fixed_now + timedelta(seconds=321)


def test_local_refactor_selects_deepseek_for_real_provider(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    deepseek_client = object()

    class CapturingOrchestrator:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def run(self, request, execution_control):
            return "completed"

    monkeypatch.setattr(local_refactor, "DeepSeekClient", lambda: deepseek_client)
    monkeypatch.setattr(local_refactor, "SQLiteRunStore", lambda path: object())
    monkeypatch.setattr(local_refactor, "RefactorOrchestrator", CapturingOrchestrator)

    result = local_refactor.run_local_refactor(
        _request(tmp_path),
        run_root=tmp_path / "runs",
        database_path=tmp_path / "runs.sqlite",
        pytest_timeout_seconds=30,
        mock=False,
    )

    assert result == "completed"
    assert captured["llm_client"] is deepseek_client


def test_local_refactor_propagates_provider_configuration_errors(monkeypatch, tmp_path: Path) -> None:
    def unavailable_deepseek():
        raise LLMError("DeepSeek configuration is missing")

    monkeypatch.setattr(local_refactor, "DeepSeekClient", unavailable_deepseek)

    with pytest.raises(
        LocalRefactorConfigurationError,
        match="DeepSeek configuration is missing",
    ) as raised:
        local_refactor.run_local_refactor(
            _request(tmp_path),
            run_root=tmp_path / "runs",
            database_path=tmp_path / "runs.sqlite",
            pytest_timeout_seconds=30,
            mock=False,
        )

    assert raised.value.code is ErrorCode.INTERNAL_ERROR


def _request(tmp_path: Path) -> RefactorRequest:
    return RefactorRequest(
        target_file=tmp_path / "target.py",
        issue_text="Simplify the function",
        tests_path=tmp_path / "tests",
    )
