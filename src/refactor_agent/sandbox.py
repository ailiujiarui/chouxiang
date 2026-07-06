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
    return run_pytest_with_backend(
        workspace=workspace,
        tests_path=tests_path,
        timeout_seconds=timeout_seconds,
        backend="subprocess",
    )


def run_pytest_with_backend(
    workspace: Path,
    tests_path: Path,
    timeout_seconds: float = 30.0,
    backend: str = "subprocess",
    docker_image: str = "refactor-agent-sandbox:py312",
    memory: str = "256m",
    cpus: float = 1.0,
) -> SandboxResult:
    if backend == "docker" or (backend == "auto" and docker_available()):
        return _run_pytest_docker(workspace, tests_path, timeout_seconds, docker_image, memory, cpus)
    if backend not in {"subprocess", "auto"}:
        raise ValueError(f"Unsupported sandbox backend: {backend}")
    return _run_pytest_subprocess(workspace, tests_path, timeout_seconds)


def _run_pytest_subprocess(workspace: Path, tests_path: Path, timeout_seconds: float = 30.0) -> SandboxResult:
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
    return run_performance_profile_with_backend(
        workspace=workspace,
        target_file=target_file,
        tests_path=tests_path,
        timeout_seconds=timeout_seconds,
        backend="subprocess",
    )


def run_performance_profile_with_backend(
    workspace: Path,
    target_file: Path,
    tests_path: Path,
    timeout_seconds: float = 30.0,
    backend: str = "subprocess",
    docker_image: str = "refactor-agent-sandbox:py312",
    memory: str = "256m",
    cpus: float = 1.0,
) -> PerformanceProfile:
    if backend == "docker" or (backend == "auto" and docker_available()):
        return _run_performance_profile_docker(
            workspace,
            target_file,
            tests_path,
            timeout_seconds,
            docker_image,
            memory,
            cpus,
        )
    if backend not in {"subprocess", "auto"}:
        raise ValueError(f"Unsupported sandbox backend: {backend}")
    return _run_performance_profile_subprocess(workspace, target_file, tests_path, timeout_seconds)


def _run_performance_profile_subprocess(
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


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def build_docker_command(
    workspace: Path,
    docker_image: str,
    memory: str,
    cpus: float,
    inner_command: str,
) -> list[str]:
    workspace = workspace.resolve()
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-v",
        f"{workspace.as_posix()}:/workspace",
        "-w",
        "/workspace",
        docker_image,
        "python",
        "-c",
        inner_command,
    ]


def _run_pytest_docker(
    workspace: Path,
    tests_path: Path,
    timeout_seconds: float,
    docker_image: str,
    memory: str,
    cpus: float,
) -> SandboxResult:
    start = time.perf_counter()
    relative_tests = tests_path.resolve().relative_to(workspace.resolve()).as_posix()
    script = (
        "import pytest, sys; "
        f"sys.exit(pytest.main([{relative_tests!r}]))"
    )
    try:
        completed = subprocess.run(
            build_docker_command(workspace, docker_image, memory, cpus, script),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - start
        return SandboxResult(
            passed=False,
            returncode=124,
            stdout=_decode_timeout_stream(exc.stdout),
            stderr=f"docker pytest timed out after {timeout_seconds} seconds\n{_decode_timeout_stream(exc.stderr)}",
            duration_seconds=duration,
        )
    duration = time.perf_counter() - start
    return SandboxResult(
        passed=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=duration,
    )


def _run_performance_profile_docker(
    workspace: Path,
    target_file: Path,
    tests_path: Path,
    timeout_seconds: float,
    docker_image: str,
    memory: str,
    cpus: float,
) -> PerformanceProfile:
    workspace = workspace.resolve()
    target_arg = Path("/workspace") / target_file.resolve().relative_to(workspace)
    tests_arg = Path("/workspace") / tests_path.resolve().relative_to(workspace)
    try:
        completed = subprocess.run(
            build_docker_command(
                workspace,
                docker_image,
                memory,
                cpus,
                _performance_script(),
            )
            + ["/workspace", str(target_arg).replace("\\", "/"), str(tests_arg).replace("\\", "/")],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return PerformanceProfile(
            passed=False,
            pytest_returncode=124,
            pytest_duration_seconds=timeout_seconds,
            peak_memory_kib=0.0,
            stdout=_decode_timeout_stream(exc.stdout),
            stderr=f"docker performance profiling timed out after {timeout_seconds} seconds\n{_decode_timeout_stream(exc.stderr)}",
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
            stderr=completed.stderr or "docker performance profiler did not emit JSON",
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
