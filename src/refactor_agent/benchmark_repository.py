from __future__ import annotations

import re
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from refactor_agent.benchmark_manifest import BenchmarkCase


RepositoryRunner = Callable[[list[str], Path, float], subprocess.CompletedProcess[str]]


class BenchmarkRepositoryError(RuntimeError):
    pass


class BenchmarkRepositoryCache:
    def __init__(
        self,
        cache_root: Path,
        runner: RepositoryRunner | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.cache_root = cache_root.resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.runner = runner or self._run
        self.timeout_seconds = timeout_seconds

    def prepare(self, case: BenchmarkCase, destination: Path) -> Path:
        owner, repo = case.repository.split("/", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner + repo):
            raise BenchmarkRepositoryError("invalid benchmark repository identity")
        canonical = f"https://github.com/{case.repository}.git"
        bare = self.cache_root / owner / f"{repo}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        if not bare.exists():
            self._command(["git", "clone", "--bare", canonical, str(bare)], self.cache_root)
        origin = self._command(["git", "config", "--get", "remote.origin.url"], bare).stdout.strip()
        if origin != canonical or "@" in origin:
            raise BenchmarkRepositoryError("benchmark cache origin is not canonical anonymous GitHub HTTPS")
        self._command(["git", "fetch", "origin", case.commit], bare)
        self._command(["git", "cat-file", "-e", f"{case.commit}^{{commit}}"], bare)
        if destination.exists() and any(destination.iterdir()):
            raise BenchmarkRepositoryError(f"benchmark destination is not empty: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._command(["git", "clone", "--no-checkout", str(bare), str(destination)], destination.parent)
        self._command(["git", "remote", "set-url", "origin", canonical], destination)
        self._command(["git", "checkout", "--detach", case.commit], destination)
        return destination

    def _command(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        completed = self.runner(command, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise BenchmarkRepositoryError(detail[:2048])
        return completed

    @staticmethod
    def _run(command: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_anonymous_git_env(),
        )


def _anonymous_git_env() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TMP", "TEMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return env
