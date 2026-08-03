"""Runtime capability decisions and the public control API capability document."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from refactor_agent.config import AppSettings
from refactor_agent.sqlite_runtime import SQLiteDiagnostics


def runtime_capabilities(
    settings: AppSettings,
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    environment = os.environ if environ is None else environ
    deepseek_available = bool(environment.get("DEEPSEEK_API_KEY"))
    llm_available = settings.mock_llm or deepseek_available
    docker_available = settings.sandbox_backend == "docker"
    return {
        "deepseek_available": deepseek_available,
        "llm_available": llm_available,
        "url_submission": docker_available and llm_available,
        "snippet_submission": llm_available,
        "snippet_verified_refactor": docker_available and llm_available,
    }


def product_mode(settings: AppSettings) -> Literal["deepseek", "demo"]:
    return "demo" if settings.mock_llm else "deepseek"


def build_capabilities_response(
    settings: AppSettings,
    sqlite_diagnostics: SQLiteDiagnostics,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    mode = product_mode(settings)
    return {
        "sandbox_backend": settings.sandbox_backend,
        "graph_backend": settings.graph_backend,
        "llm_mode": "mock" if settings.mock_llm else settings.llm_provider,
        "product_mode": mode,
        "demo_limitations": (
            "Deterministic demo supports only built-in patterns; arbitrary code requires DeepSeek."
            if mode == "demo"
            else None
        ),
        **runtime_capabilities(settings, environ),
        "snippet_modes": ["REVIEW", "VERIFIED_REFACTOR"],
        "personas": ["STRICT", "TSUNDERE"],
        "admin_token_required": bool(settings.admin_token),
        "sqlite": sqlite_diagnostics.as_public_dict(),
    }
