from __future__ import annotations

from pathlib import Path

from radon.complexity import cc_visit

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
    blocks = cc_visit(source) if source.strip() else []
    details = [
        {
            "name": block.name,
            "type": block.__class__.__name__,
            "lineno": block.lineno,
            "complexity": block.complexity,
        }
        for block in blocks
    ]
    return MetricsSnapshot(
        loc=count_logical_loc(source),
        cyclomatic_complexity=sum(item["complexity"] for item in details),
        details=details,
    )


def analyze_file(path: Path) -> MetricsSnapshot:
    return analyze_source(path.read_text(encoding="utf-8"))
