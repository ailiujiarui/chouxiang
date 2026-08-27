from __future__ import annotations

import ast
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[1]

# nailong may only consume refactor_agent through these documented seams.
_NAILONG_ALLOWED_REFACTOR_PREFIXES = (
    "refactor_agent.analysis_events",
    "refactor_agent.sqlite_runtime",
    "refactor_agent.artifacts",
    "refactor_agent.ast_analyzer",
    "refactor_agent.llm",
)


def _module_files(package: str) -> list[Path]:
    root = _PKG_ROOT / package
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def test_refactor_agent_never_imports_nailong_agent() -> None:
    """Dependency direction is one-way: nailong depends on refactor_agent, never the reverse."""
    violations: list[str] = []
    for path in _module_files("refactor_agent"):
        tree = _parse(path)
        for module in _imported_modules(tree):
            if module == "nailong_agent" or module.startswith("nailong_agent."):
                violations.append(f"{path.name}: imports {module!r}")
    assert violations == []


def test_nailong_only_imports_refactor_agent_through_documented_seams() -> None:
    """Keep the desktop package decoupled from engine internals.

    A future physical split is mechanical: move src/nailong_agent to a sibling
    package that declares `refactor-agent` as a dependency.  This test keeps
    the import surface limited to the shared contracts and the lazy LLM seam.
    """
    violations: list[str] = []
    for path in _module_files("nailong_agent"):
        tree = _parse(path)
        for module in _imported_modules(tree):
            if module != "refactor_agent" and not module.startswith("refactor_agent."):
                continue
            if module == "refactor_agent":
                continue
            if not any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _NAILONG_ALLOWED_REFACTOR_PREFIXES
            ):
                violations.append(f"{path.name}: imports {module!r}")
    assert violations == []
