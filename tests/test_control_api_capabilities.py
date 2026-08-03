from __future__ import annotations

import pytest

from refactor_agent.config import AppSettings
from refactor_agent.control_api_capabilities import (
    build_capabilities_response,
    product_mode,
    runtime_capabilities,
)
from refactor_agent.sqlite_runtime import SQLiteDiagnostics


@pytest.mark.parametrize(
    ("mock_llm", "api_key", "sandbox_backend", "expected"),
    [
        (
            False,
            None,
            "docker",
            {
                "deepseek_available": False,
                "llm_available": False,
                "url_submission": False,
                "snippet_submission": False,
                "snippet_verified_refactor": False,
            },
        ),
        (
            False,
            "test-key",
            "docker",
            {
                "deepseek_available": True,
                "llm_available": True,
                "url_submission": True,
                "snippet_submission": True,
                "snippet_verified_refactor": True,
            },
        ),
        (
            True,
            None,
            "subprocess",
            {
                "deepseek_available": False,
                "llm_available": True,
                "url_submission": False,
                "snippet_submission": True,
                "snippet_verified_refactor": False,
            },
        ),
        (
            False,
            "test-key",
            "subprocess",
            {
                "deepseek_available": True,
                "llm_available": True,
                "url_submission": False,
                "snippet_submission": True,
                "snippet_verified_refactor": False,
            },
        ),
    ],
)
def test_runtime_capabilities_preserve_llm_and_sandbox_matrix(
    mock_llm: bool,
    api_key: str | None,
    sandbox_backend: str,
    expected: dict[str, bool],
) -> None:
    settings = AppSettings(mock_llm=mock_llm, sandbox_backend=sandbox_backend)
    environ = {"DEEPSEEK_API_KEY": api_key} if api_key is not None else {}

    assert runtime_capabilities(settings, environ) == expected


def test_capability_response_preserves_public_fields_and_safe_sqlite_diagnostics() -> None:
    settings = AppSettings(
        admin_token="admin-secret",
        sandbox_backend="docker",
        graph_backend="langgraph",
        llm_provider="deepseek",
        mock_llm=True,
    )
    diagnostics = SQLiteDiagnostics(
        sqlite_version="3.51.2",
        requested_journal_mode="auto",
        actual_journal_mode="delete",
        busy_timeout_ms=5000,
        foreign_keys=True,
        wal_safe=False,
        wal_gate_reason="sqlite_version_not_fixed",
    )

    response = build_capabilities_response(settings, diagnostics, environ={})

    assert response == {
        "sandbox_backend": "docker",
        "graph_backend": "langgraph",
        "llm_mode": "mock",
        "product_mode": "demo",
        "demo_limitations": (
            "Deterministic demo supports only built-in patterns; arbitrary code requires DeepSeek."
        ),
        "deepseek_available": False,
        "llm_available": True,
        "url_submission": True,
        "snippet_submission": True,
        "snippet_verified_refactor": True,
        "snippet_modes": ["REVIEW", "VERIFIED_REFACTOR"],
        "personas": ["STRICT", "TSUNDERE"],
        "admin_token_required": True,
        "sqlite": diagnostics.as_public_dict(),
    }
    assert product_mode(settings) == "demo"
    assert product_mode(settings.model_copy(update={"mock_llm": False})) == "deepseek"
