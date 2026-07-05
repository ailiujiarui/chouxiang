from __future__ import annotations

import shutil
import subprocess
import sys
import time
import os
from pathlib import Path

from refactor_agent.models import SandboxResult


IGNORED_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".runs",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def infer_source_root(target_file: Path, tests_path: Path) -> Path:
    target = target_file.resolve()
    tests = tests_path.resolve()
    common = Path(os.path.commonpath([str(target), str(tests)]))
    if common == Path(common.anchor):
        raise ValueError("target_file and tests_path must share a non-root project directory.")
    if common == target:
        return target.parent
    return common


def prepare_workspace(target_file: Path, tests_path: Path, workspace: Path) -> tuple[Path, Path, Path]:
    source_root = infer_source_root(target_file, tests_path)
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, workspace, dirs_exist_ok=True, ignore=_ignore)
    target_in_workspace = workspace / target_file.resolve().relative_to(source_root)
    tests_in_workspace = workspace / tests_path.resolve().relative_to(source_root)
    return source_root, target_in_workspace, tests_in_workspace


def write_candidate(target_in_workspace: Path, fixed_code: str) -> None:
    target_in_workspace.parent.mkdir(parents=True, exist_ok=True)
    target_in_workspace.write_text(fixed_code, encoding="utf-8")


def run_pytest(workspace: Path, tests_path: Path, timeout_seconds: float = 30.0) -> SandboxResult:
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        duration = time.perf_counter() - start
        return SandboxResult(
            passed=completed.returncode == 0,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - start
        return SandboxResult(
            passed=False,
            returncode=124,
            stdout=_decode_timeout_stream(exc.stdout),
            stderr=f"pytest timed out after {timeout_seconds} seconds\n{_decode_timeout_stream(exc.stderr)}",
            duration_seconds=duration,
        )


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_NAMES}


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
