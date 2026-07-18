from __future__ import annotations

import json
import os
import re
import tempfile
from difflib import unified_diff
from pathlib import Path, PurePath
from typing import Any


ARTIFACT_NAMES = {
    "original.py",
    "candidate.py",
    "change.diff",
    "pytest.log",
    "adversary.log",
    "mutation.json",
    "report.md",
}

_NAMED_SECRET = re.compile(
    r"(?i)\b(GITHUB_TOKEN|DEEPSEEK_API_KEY|GITHUB_WEBHOOK_SECRET|"
    r"REFACTOR_AGENT_ADMIN_TOKEN)\s*[:=]\s*[^\s]+"
)
_BEARER_SECRET = re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+|Bearer\s+)[A-Za-z0-9._~+/-]{12,}")
_TOKEN_SECRET = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,})\b")


def sanitize_text(value: str) -> str:
    sanitized = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    sanitized = _BEARER_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", sanitized)
    return _TOKEN_SECRET.sub("[REDACTED]", sanitized)


def sanitize_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_data(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def resolve_artifact_path(run_root: Path, run_id: str, artifact_name: str) -> Path:
    if artifact_name not in ARTIFACT_NAMES:
        raise ValueError(f"invalid artifact name: {artifact_name}")
    if len(PurePath(run_id).parts) != 1 or run_id in {"", ".", ".."}:
        raise ValueError(f"invalid artifact run id: {run_id}")
    root = run_root.resolve()
    artifact_root = (root / run_id / "artifacts").resolve()
    if not artifact_root.is_relative_to(root):
        raise ValueError("artifact directory escapes run root")
    candidate = artifact_root / artifact_name
    resolved = candidate.resolve()
    if not resolved.is_relative_to(artifact_root):
        raise ValueError("artifact path escapes artifact directory")
    return resolved


class RunArtifactWriter:
    def __init__(self, run_dir: Path, max_log_bytes: int = 256 * 1024) -> None:
        self.run_dir = run_dir.resolve()
        self.artifact_root = self.run_dir / "artifacts"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.max_log_bytes = max_log_bytes

    def write_sources(self, original: str, candidate: str) -> None:
        self._write("original.py", sanitize_text(original))
        self._write("candidate.py", sanitize_text(candidate))
        diff = "".join(
            unified_diff(
                original.splitlines(keepends=True),
                candidate.splitlines(keepends=True),
                fromfile="original.py",
                tofile="candidate.py",
            )
        )
        self._write("change.diff", sanitize_text(diff))

    def write_log(self, name: str, value: str) -> None:
        if name not in {"pytest.log", "adversary.log"}:
            raise ValueError(f"invalid log artifact: {name}")
        self._write(name, _truncate_utf8(sanitize_text(value), self.max_log_bytes))

    def write_json(self, name: str, value: Any) -> None:
        if name != "mutation.json":
            raise ValueError(f"invalid JSON artifact: {name}")
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._write(name, _truncate_utf8(sanitize_text(payload), self.max_log_bytes))

    def write_report(self, report: str) -> None:
        self._write("report.md", _truncate_utf8(sanitize_text(report), self.max_log_bytes))

    def _write(self, name: str, value: str) -> None:
        if name not in ARTIFACT_NAMES:
            raise ValueError(f"invalid artifact name: {name}")
        destination = self.artifact_root / name
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=self.artifact_root,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
        os.replace(temporary, destination)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
