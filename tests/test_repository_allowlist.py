from pathlib import Path

import pytest

from refactor_agent.config import AppSettings
from refactor_agent.repository_allowlist import (
    EnvironmentRepositoryRemovalError,
    RepositoryAllowlistLimitError,
    RepositoryAllowlistPolicy,
    normalize_repository_identity,
)
from refactor_agent.store import SQLiteRunStore


def test_policy_merges_environment_and_persisted_entries(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    policy = RepositoryAllowlistPolicy(
        AppSettings(allowed_repositories={"Octo/Environment"}),
        store,
    )

    environment = policy.add("https://github.com/OCTO/environment")
    dashboard = policy.add("Octo/Dashboard")
    reloaded = RepositoryAllowlistPolicy(
        AppSettings(allowed_repositories={"octo/environment"}),
        SQLiteRunStore(tmp_path / "runs.sqlite"),
    )

    assert environment.source == "ENVIRONMENT"
    assert environment.removable is False
    assert store.count_repository_allowlist_entries() == 1
    assert dashboard.repo_full_name == "octo/dashboard"
    assert [(entry.repo_full_name, entry.source, entry.removable) for entry in reloaded.list_entries()] == [
        ("octo/dashboard", "DASHBOARD", True),
        ("octo/environment", "ENVIRONMENT", False),
    ]
    assert reloaded.is_allowed("OCTO/DASHBOARD") is True
    assert reloaded.is_allowed("octo/missing") is False


def test_policy_protects_environment_entries_and_enforces_dashboard_limit(tmp_path: Path):
    policy = RepositoryAllowlistPolicy(
        AppSettings(allowed_repositories={"octo/environment"}),
        SQLiteRunStore(tmp_path / "runs.sqlite"),
        max_dashboard_entries=1,
    )
    policy.add("octo/first")

    with pytest.raises(EnvironmentRepositoryRemovalError):
        policy.remove("octo/environment")
    with pytest.raises(RepositoryAllowlistLimitError):
        policy.add("octo/second")

    assert policy.remove("octo/first") is True
    assert policy.remove("octo/first") is False


@pytest.mark.parametrize(
    "repository",
    [
        "octo/demo",
        "OCTO/Demo",
        "https://github.com/Octo/Demo",
        "https://github.com/octo/demo.git",
    ],
)
def test_normalize_repository_identity_accepts_canonical_values(repository: str):
    assert normalize_repository_identity(repository) == "octo/demo"


@pytest.mark.parametrize(
    "repository",
    [
        "*/*",
        "octo",
        "octo/demo/extra",
        "git@github.com:octo/demo.git",
        "https://example.com/octo/demo",
        "https://user:token@github.com/octo/demo",
        "https://github.com:8443/octo/demo",
        "https://github.com/octo/demo?ref=main",
        "https://github.com/octo/demo#readme",
        "https://github.com/octo//demo",
        "../demo",
    ],
)
def test_normalize_repository_identity_rejects_unsafe_values(repository: str):
    with pytest.raises(ValueError):
        normalize_repository_identity(repository)
