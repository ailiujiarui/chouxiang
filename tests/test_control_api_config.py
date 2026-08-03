from __future__ import annotations

from types import SimpleNamespace

import pytest

import refactor_agent.control_api_config as config_validation
from refactor_agent.config import AppSettings
from refactor_agent.control_api import validate_control_api_settings as control_api_validate
from refactor_agent.repository_allowlist import RepositoryAllowlistPolicy
from refactor_agent.store import SQLiteRunStore
from refactor_agent.webhook import validate_control_api_settings as webhook_validate


def test_validation_is_fail_closed_without_repository_allowlist() -> None:
    settings = AppSettings(sandbox_backend="docker")

    with pytest.raises(
        RuntimeError,
        match="Control API configuration is fail-closed; missing: "
        "REFACTOR_AGENT_ALLOWED_REPOSITORIES",
    ):
        config_validation.validate_control_api_settings(settings)


def test_dashboard_allowlist_entry_satisfies_fail_closed_validation(tmp_path) -> None:
    settings = AppSettings(sandbox_backend="docker")
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    repository_policy = RepositoryAllowlistPolicy(settings, store)
    repository_policy.add("Example/Repository")

    config_validation.validate_control_api_settings(
        settings,
        repository_policy=repository_policy,
    )


def test_validation_requires_docker_backend() -> None:
    settings = AppSettings(
        allowed_repositories={"example/repository"},
        sandbox_backend="subprocess",
    )

    with pytest.raises(
        RuntimeError,
        match="Control API requires REFACTOR_AGENT_SANDBOX_BACKEND=docker\\.",
    ):
        config_validation.validate_control_api_settings(settings)


def test_validation_checks_daemon_only_when_worker_requires_it(monkeypatch) -> None:
    settings = AppSettings(
        allowed_repositories={"example/repository"},
        sandbox_backend="docker",
    )

    def unavailable_daemon():
        return SimpleNamespace(available=False, error="daemon unavailable")

    monkeypatch.setattr(config_validation, "docker_status", unavailable_daemon)

    config_validation.validate_control_api_settings(settings, require_docker=False)
    with pytest.raises(
        RuntimeError,
        match="Control API requires an available Docker daemon: daemon unavailable",
    ):
        config_validation.validate_control_api_settings(settings, require_docker=True)


def test_legacy_validation_import_paths_remain_compatible() -> None:
    assert webhook_validate is config_validation.validate_control_api_settings
    assert control_api_validate is config_validation.validate_control_api_settings
