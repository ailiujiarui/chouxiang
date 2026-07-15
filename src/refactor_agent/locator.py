from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from refactor_agent.ast_analyzer import analyze_ast


AUTO_TARGET_PATH = "__auto__"
IGNORED_PARTS = {
    ".git",
    ".github-workspaces",
    ".mypy_cache",
    ".pytest_cache",
    ".runs",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tests",
    "venv",
}


class LocatedFile(BaseModel):
    path: str
    score: int
    reason: str


def locate_source_file(repo_root: Path, issue_text: str, min_score: int = 20) -> LocatedFile | None:
    candidates = [_score_file(repo_root, path, issue_text) for path in _python_files(repo_root)]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if not candidates:
        return None
    best = max(candidates, key=lambda item: item.score)
    return best if best.score >= min_score else None


def _python_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo_root.rglob("*.py"):
        relative_parts = path.relative_to(repo_root).parts
        if any(part in IGNORED_PARTS for part in relative_parts):
            continue
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        files.append(path)
    return files


def _score_file(repo_root: Path, path: Path, issue_text: str) -> LocatedFile | None:
    rel = path.relative_to(repo_root).as_posix()
    issue_lower = issue_text.lower()
    issue_tokens = _tokens(issue_text)
    reasons: list[str] = []
    score = 0

    if rel.lower() in issue_lower:
        score += 100
        reasons.append("explicit relative path mention")
    if path.name.lower() in issue_lower:
        score += 60
        reasons.append("filename mention")

    stem_tokens = _tokens(path.stem.replace("_", " "))
    overlap = sorted(stem_tokens & issue_tokens)
    if overlap:
        score += len(overlap) * 15
        reasons.append(f"filename token overlap: {', '.join(overlap)}")

    try:
        analysis = analyze_ast(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None

    symbol_hits = []
    for symbol in analysis.public_symbols:
        symbol_tokens = _tokens(symbol.replace("_", " "))
        if symbol.lower() in issue_lower or symbol_tokens <= issue_tokens:
            score += 30
            symbol_hits.append(symbol)
    for function in analysis.functions:
        function_tokens = _tokens(function.name.replace("_", " "))
        if function.name.lower() in issue_lower or function_tokens <= issue_tokens:
            score += 20
            symbol_hits.append(function.name)
    if symbol_hits:
        reasons.append(f"AST symbol match: {', '.join(sorted(set(symbol_hits)))}")

    if score == 0:
        return None
    return LocatedFile(path=rel, score=score, reason="; ".join(reasons))


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", value)}
