from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import sys
import time
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


@dataclass(frozen=True)
class DockerStatus:
    available: bool
    executable: str | None = None
    server_version: str | None = None
    error: str | None = None


class SandboxUnavailableError(RuntimeError):
    pass


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
    resolved_backend, docker = resolve_sandbox_backend(backend)
    if resolved_backend == "docker":
        return _run_pytest_docker(
            workspace,
            tests_path,
            timeout_seconds,
            docker_image,
            memory,
            cpus,
            docker.executable if docker else "docker",
        )
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
    resolved_backend, docker = resolve_sandbox_backend(backend)
    if resolved_backend == "docker":
        return _run_performance_profile_docker(
            workspace,
            target_file,
            tests_path,
            timeout_seconds,
            docker_image,
            memory,
            cpus,
            docker.executable if docker else "docker",
        )
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


def resolve_sandbox_backend(backend: str) -> tuple[str, DockerStatus | None]:
    if backend not in {"subprocess", "docker", "auto"}:
        raise ValueError(f"Unsupported sandbox backend: {backend}")
    if backend == "subprocess":
        return "subprocess", None

    status = docker_status()
    if status.available:
        return "docker", status
    if backend == "auto":
        return "subprocess", status
    raise SandboxUnavailableError(_docker_unavailable_message(status))


def docker_status(timeout_seconds: float = 5.0) -> DockerStatus:
    executable = find_docker_executable()
    if executable is None:
        return DockerStatus(
            available=False,
            error=(
                "Docker CLI was not found on PATH. Install Docker Desktop or add "
                "docker.exe to PATH."
            ),
        )
    try:
        completed = subprocess.run(
            [executable, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return DockerStatus(
            available=False,
            executable=executable,
            error=(
                "Docker CLI was found, but the Docker daemon did not respond within "
                f"{timeout_seconds:g} seconds."
            ),
        )
    except OSError as exc:
        return DockerStatus(available=False, executable=executable, error=str(exc))

    if completed.returncode != 0:
        error = "\n".join(part for part in [completed.stderr.strip(), completed.stdout.strip()] if part)
        return DockerStatus(
            available=False,
            executable=executable,
            error=error or f"docker info exited with code {completed.returncode}.",
        )
    return DockerStatus(
        available=True,
        executable=executable,
        server_version=completed.stdout.strip(),
    )


def find_docker_executable() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    if os.name == "nt":
        default_path = Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe")
        if default_path.is_file():
            return str(default_path)
    return None


def docker_available() -> bool:
    return docker_status().available


def build_docker_command(
    workspace: Path,
    docker_image: str,
    memory: str,
    cpus: float,
    inner_command: str,
    docker_executable: str = "docker",
    pids_limit: int = 128,
) -> list[str]:
    workspace = workspace.resolve()
    return [
        docker_executable,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(pids_limit),
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "PYTHONPYCACHEPREFIX=/tmp/pycache",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{workspace.as_posix()}:/workspace:ro",
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
    docker_executable: str = "docker",
) -> SandboxResult:
    start = time.perf_counter()
    relative_tests = tests_path.resolve().relative_to(workspace.resolve()).as_posix()
    script = (
        "import pytest, sys; "
        f"sys.exit(pytest.main([{relative_tests!r}]))"
    )
    try:
        completed = subprocess.run(
            build_docker_command(workspace, docker_image, memory, cpus, script, docker_executable),
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
    docker_executable: str = "docker",
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
                docker_executable,
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
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    return env


def _docker_unavailable_message(status: DockerStatus) -> str:
    details = status.error or "Docker is not available."
    hint = (
        "If Docker Desktop reports that virtualization support was not detected, "
        "enable Intel VT-x/AMD-V/SVM in BIOS or UEFI and make sure WSL can start."
    )
    executable = f" Executable: {status.executable}." if status.executable else ""
    return f"Docker sandbox requested but unavailable.{executable} {details} {hint}"


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
