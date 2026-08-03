from __future__ import annotations

from refactor_agent.config import AppSettings
from refactor_agent.repository_allowlist import RepositoryAllowlistPolicy
from refactor_agent.sandbox import docker_status


def validate_control_api_settings(
    settings: AppSettings,
    require_docker: bool = False,
    repository_policy: RepositoryAllowlistPolicy | None = None,
) -> None:
    missing = []
    if not (
        repository_policy.list_entries()
        if repository_policy is not None
        else settings.allowed_repositories
    ):
        missing.append("REFACTOR_AGENT_ALLOWED_REPOSITORIES")
    if missing:
        raise RuntimeError("Control API configuration is fail-closed; missing: " + ", ".join(missing))
    if settings.sandbox_backend != "docker":
        raise RuntimeError("Control API requires REFACTOR_AGENT_SANDBOX_BACKEND=docker.")
    if require_docker:
        docker = docker_status()
        if not docker.available:
            raise RuntimeError(f"Control API requires an available Docker daemon: {docker.error}")
