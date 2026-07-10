from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    github_token: str | None = None
    github_webhook_secret: str | None = None
    github_api_url: str = "https://api.github.com"
    github_workspace_root: Path = Path(".github-workspaces")
    run_root: Path = Path(".runs")
    database_path: Path | None = None
    default_tests_path: str = "tests"
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
    dry_run: bool = False
    mock_llm: bool = False

    @classmethod
    def from_env(cls) -> "AppSettings":
        return cls(
            github_token=os.getenv("GITHUB_TOKEN"),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET"),
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"),
            github_workspace_root=Path(os.getenv("REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT", ".github-workspaces")),
            run_root=Path(os.getenv("REFACTOR_AGENT_RUN_ROOT", ".runs")),
            database_path=_optional_path(os.getenv("REFACTOR_AGENT_DATABASE")),
            default_tests_path=os.getenv("REFACTOR_AGENT_TESTS_PATH", "tests"),
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
            dry_run=_env_bool("REFACTOR_AGENT_DRY_RUN", False),
            mock_llm=_env_bool("REFACTOR_AGENT_MOCK_LLM", False),
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
