from __future__ import annotations

import shutil
import subprocess
import sys
import time
import os
import json
from pathlib import Path

from refactor_agent.models import PerformanceProfile, SandboxResult


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
    cache_dir = target_in_workspace.parent / "__pycache__"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


def run_pytest(workspace: Path, tests_path: Path, timeout_seconds: float = 30.0) -> SandboxResult:
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_sandbox_env(),
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


def run_performance_profile(
    workspace: Path,
    target_file: Path,
    tests_path: Path,
    timeout_seconds: float = 30.0,
) -> PerformanceProfile:
    script = _performance_script()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(workspace), str(target_file), str(tests_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_sandbox_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return PerformanceProfile(
            passed=False,
            pytest_returncode=124,
            pytest_duration_seconds=timeout_seconds,
            peak_memory_kib=0.0,
            stdout=_decode_timeout_stream(exc.stdout),
            stderr=f"performance profiling timed out after {timeout_seconds} seconds\n{_decode_timeout_stream(exc.stderr)}",
        )

    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return PerformanceProfile(
            passed=False,
            pytest_returncode=completed.returncode,
            pytest_duration_seconds=0.0,
            peak_memory_kib=0.0,
            stdout=completed.stdout,
            stderr=completed.stderr or "performance profiler did not emit JSON",
        )
    return PerformanceProfile.model_validate(payload)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_NAMES}


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _sandbox_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _performance_script() -> str:
    return r"""
import contextlib
import importlib
import io
import json
import sys
import time
import timeit
import tracemalloc
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
target_file = Path(sys.argv[2]).resolve()
tests_path = Path(sys.argv[3]).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(workspace))

module_name = ".".join(target_file.relative_to(workspace).with_suffix("").parts)
if module_name.endswith(".__init__"):
    module_name = module_name[:-9]

import_time = None
try:
    timer = timeit.Timer(
        "importlib.invalidate_caches(); sys.modules.pop(module_name, None); importlib.import_module(module_name)",
        globals={"importlib": importlib, "sys": sys, "module_name": module_name},
    )
    import_time = min(timer.repeat(repeat=3, number=1))
except Exception:
    import_time = None

stdout_buffer = io.StringIO()
stderr_buffer = io.StringIO()
tracemalloc.start()
start = time.perf_counter()
try:
    import pytest
    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
        returncode = pytest.main([str(tests_path)])
except BaseException as exc:
    returncode = 1
    stderr_buffer.write(repr(exc))
duration = time.perf_counter() - start
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({
    "passed": returncode == 0,
    "pytest_returncode": int(returncode),
    "pytest_duration_seconds": duration,
    "peak_memory_kib": peak / 1024,
    "import_time_seconds": import_time,
    "stdout": stdout_buffer.getvalue()[-4000:],
    "stderr": stderr_buffer.getvalue()[-4000:],
}))
""".strip()
