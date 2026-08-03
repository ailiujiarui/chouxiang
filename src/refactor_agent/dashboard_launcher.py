from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Protocol


class DashboardDependencyError(RuntimeError):
    """Raised when the optional Streamlit dashboard dependency is unavailable."""


class CompletedProcessLike(Protocol):
    returncode: int


ModuleFinder = Callable[[str], object | None]
ProcessRunner = Callable[..., CompletedProcessLike]
EnvironmentFactory = Callable[[], dict[str, str]]
ReadyCallback = Callable[[str], None]


@dataclass(frozen=True)
class DashboardLaunchResult:
    url: str
    returncode: int


def launch_dashboard(
    *,
    host: str,
    port: int,
    database_path: Path,
    run_root: Path,
    api_url: str,
    script_path: Path,
    module_finder: ModuleFinder = importlib.util.find_spec,
    process_runner: ProcessRunner = subprocess.run,
    environment_factory: EnvironmentFactory = lambda: os.environ.copy(),
    executable: str = sys.executable,
    on_ready: ReadyCallback | None = None,
) -> DashboardLaunchResult:
    """Validate and launch Streamlit without depending on CLI presentation."""

    if module_finder("streamlit") is None:
        raise DashboardDependencyError(
            "Streamlit is not installed. Install it with: "
            "python -m pip install -e .[dashboard]"
        )

    environment = environment_factory()
    environment["REFACTOR_AGENT_DASHBOARD_DB"] = str(database_path)
    environment["REFACTOR_AGENT_RUN_ROOT"] = str(run_root)
    environment["REFACTOR_AGENT_API_URL"] = api_url
    url = f"http://{host}:{port}"
    if on_ready is not None:
        on_ready(url)
    completed = process_runner(
        [
            executable,
            "-m",
            "streamlit",
            "run",
            str(script_path),
            "--server.address",
            host,
            "--server.port",
            str(port),
        ],
        env=environment,
    )
    return DashboardLaunchResult(url=url, returncode=completed.returncode)
