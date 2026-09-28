"""Frozen desktop entry point for the Nailong pet.

When launched as a packaged ``Nailong.exe`` there is no repository checkout to
anchor ``.runs`` on, so the data directory defaults to a per-user location.
Configuration can be provided either through real environment variables or a
``.env`` file placed next to ``Nailong.exe`` (only ``DEEPSEEK_*``,
``NAILONG_*`` and ``REFACTOR_AGENT_*`` keys are honoured).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ALLOWED_ENV_PREFIXES = ("DEEPSEEK_", "NAILONG_", "REFACTOR_AGENT_")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Nailong"
    return Path.home() / ".nailong"


def _load_dotenv() -> None:
    env_path = _base_dir() / ".env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key.startswith(_ALLOWED_ENV_PREFIXES):
            continue
        if key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _configure_defaults() -> None:
    _load_dotenv()
    os.environ.setdefault("NAILONG_DATA_DIR", str(_default_data_dir()))


def _self_test() -> int:
    """Exercise bundled language-graph, personality, and AST paths without a GUI."""

    log_path = _base_dir() / "nailong-self-test.log"
    try:
        from nailong_agent.contracts import (
            PetClassificationHint,
            PetDecisionInput,
            RedactedActivitySignal,
        )
        from nailong_agent.personality_agent import PetPersonalityAgent
        from refactor_agent.ast_analyzer import analyze_ast

        state = PetPersonalityAgent().run(
            PetDecisionInput(
                signal=RedactedActivitySignal(source="window", application_id="code"),
                classification=PetClassificationHint(
                    activity="test_failed",
                    confidence=0.9,
                    classifier="self-test",
                ),
            )
        )
        if state.get("output") is None:
            raise RuntimeError("personality graph produced no output")
        analyze_ast("def add(a, b):\n    return a + b\n")
    except Exception as exc:  # write a log because windowed mode has no stdout
        message = f"FAIL {type(exc).__name__}: {exc}"
        try:
            log_path.write_text(message + "\n", encoding="utf-8")
        except OSError:
            pass
        return 1
    try:
        log_path.write_text("self-test OK\n", encoding="utf-8")
    except OSError:
        pass
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    _configure_defaults()
    from nailong_agent.app import main as pet_main

    return pet_main()


if __name__ == "__main__":
    raise SystemExit(main())
