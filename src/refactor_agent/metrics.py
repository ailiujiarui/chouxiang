from __future__ import annotations

from pathlib import Path

from refactor_agent.ast_analyzer import analyze_ast
from refactor_agent.models import MetricsSnapshot


def count_logical_loc(source: str) -> int:
    """Count non-empty, non-comment source lines."""
    loc = 0
    for line in source.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            loc += 1
    return loc


def analyze_source(source: str) -> MetricsSnapshot:
    analysis = analyze_ast(source) if source.strip() else None
    details = [] if analysis is None else [
        {
            "name": item.qualified_name,
            "type": "FunctionDef",
            "lineno": item.lineno,
            "end_lineno": item.end_lineno,
            "complexity": item.complexity,
        }
        for item in [
            *analysis.functions,
            *(method for class_summary in analysis.classes for method in class_summary.methods),
        ]
    ]
    return MetricsSnapshot(
        loc=count_logical_loc(source),
        cyclomatic_complexity=0 if analysis is None else analysis.cyclomatic_complexity,
        details=details,
    )


def analyze_file(path: Path) -> MetricsSnapshot:
    return analyze_source(path.read_text(encoding="utf-8"))
