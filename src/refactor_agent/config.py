from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    admin_token: str | None = None
    allowed_repositories: set[str] = Field(default_factory=set)
    allowed_import_roots: set[str] = Field(default_factory=set)
    github_workspace_root: Path = Path(".github-workspaces")
    run_root: Path = Path(".runs")
    database_path: Path | None = None
    max_retry: int = Field(default=3, ge=1)
    pytest_timeout_seconds: float = Field(default=30.0, gt=0)
    sandbox_backend: str = "subprocess"
    sandbox_docker_image: str = "refactor-agent-sandbox:py312"
    sandbox_memory: str = "256m"
    sandbox_cpus: float = Field(default=1.0, gt=0)
    graph_backend: str = "langgraph"
    llm_provider: str = "deepseek"
    docker_driver: str = "sdk"
    db_driver: str = "sqlalchemy"
    mock_llm: bool = False
    retain_checkouts: bool = False
    job_lease_seconds: int = Field(default=300, ge=30, le=3600)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_deadline_seconds: int = Field(default=900, ge=30, le=7200)
    request_max_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)

    @classmethod
    def from_env(cls) -> "AppSettings":
        return cls(
            admin_token=os.getenv("REFACTOR_AGENT_ADMIN_TOKEN"),
            allowed_repositories=_csv_set(os.getenv("REFACTOR_AGENT_ALLOWED_REPOSITORIES")),
            allowed_import_roots=_csv_set(os.getenv("REFACTOR_AGENT_ALLOWED_IMPORTS")),
            github_workspace_root=Path(os.getenv("REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT", ".github-workspaces")),
            run_root=Path(os.getenv("REFACTOR_AGENT_RUN_ROOT", ".runs")),
            database_path=_optional_path(os.getenv("REFACTOR_AGENT_DATABASE")),
            max_retry=int(os.getenv("REFACTOR_AGENT_MAX_RETRY", "3")),
            pytest_timeout_seconds=float(os.getenv("REFACTOR_AGENT_PYTEST_TIMEOUT", "30")),
            sandbox_backend=os.getenv("REFACTOR_AGENT_SANDBOX_BACKEND", "subprocess"),
            sandbox_docker_image=os.getenv("REFACTOR_AGENT_SANDBOX_DOCKER_IMAGE", "refactor-agent-sandbox:py312"),
            sandbox_memory=os.getenv("REFACTOR_AGENT_SANDBOX_MEMORY", "256m"),
            sandbox_cpus=float(os.getenv("REFACTOR_AGENT_SANDBOX_CPUS", "1.0")),
            graph_backend=os.getenv("REFACTOR_AGENT_GRAPH_BACKEND", "langgraph"),
            llm_provider=os.getenv("REFACTOR_AGENT_LLM_PROVIDER", "deepseek"),
            docker_driver=os.getenv("REFACTOR_AGENT_DOCKER_DRIVER", "sdk"),
            db_driver=os.getenv("REFACTOR_AGENT_DB_DRIVER", "sqlalchemy"),
            mock_llm=_env_bool("REFACTOR_AGENT_MOCK_LLM", False),
            retain_checkouts=_env_bool("REFACTOR_AGENT_RETAIN_CHECKOUTS", False),
            job_lease_seconds=int(os.getenv("REFACTOR_AGENT_JOB_LEASE_SECONDS", "300")),
            job_max_attempts=int(os.getenv("REFACTOR_AGENT_JOB_MAX_ATTEMPTS", "3")),
            job_deadline_seconds=int(os.getenv("REFACTOR_AGENT_JOB_DEADLINE_SECONDS", "900")),
            request_max_bytes=int(os.getenv("REFACTOR_AGENT_REQUEST_MAX_BYTES", "1048576")),
        )

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path or (self.run_root / "refactor_agent.sqlite")


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(value: str | None) -> set[str]:
    return {item.strip().lower() for item in (value or "").split(",") if item.strip()}
