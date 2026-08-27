from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol


class LLMProvider(Protocol):
    """Minimal JSON-completion boundary used by Nailong reasoning components.

    Structural protocol: any object exposing ``complete_json`` satisfies it,
    so the desktop package never depends on a concrete model client.
    """

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> dict[str, object]:
        ...


def deepseek_provider_factory(
    model: str | None,
    *,
    require_api_key: bool = False,
) -> Callable[[], LLMProvider | None]:
    """Return a factory that lazily builds the DeepSeek-backed provider.

    The concrete client is imported lazily so Nailong package modules and
    entrypoints never depend on ``refactor_agent.llm`` internals at import
    time.  ``require_api_key`` mirrors the caller's production policy: when
    true the factory returns ``None`` (LLM unavailable) without an API key.
    """

    def _factory() -> LLMProvider | None:
        if require_api_key and not os.getenv("DEEPSEEK_API_KEY"):
            return None
        from refactor_agent.llm import DeepSeekClient

        return DeepSeekClient(model=model)

    return _factory
