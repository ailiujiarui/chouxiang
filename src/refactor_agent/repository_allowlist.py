from __future__ import annotations

import re
from urllib.parse import urlsplit

from refactor_agent.config import AppSettings
from refactor_agent.models import RepositoryAllowlistEntry
from refactor_agent.store import SQLiteRunStore


MAX_DASHBOARD_ALLOWLIST_ENTRIES = 500
_REPOSITORY_PART_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")


class RepositoryNotAllowlistedError(ValueError):
    pass


class EnvironmentRepositoryRemovalError(ValueError):
    pass


class RepositoryAllowlistLimitError(ValueError):
    pass


class RepositoryAllowlistPolicy:
    def __init__(
        self,
        settings: AppSettings,
        store: SQLiteRunStore,
        max_dashboard_entries: int = MAX_DASHBOARD_ALLOWLIST_ENTRIES,
    ) -> None:
        self.environment_repositories = {
            normalize_repository_identity(item) for item in settings.allowed_repositories
        }
        self.store = store
        self.max_dashboard_entries = max_dashboard_entries

    def list_entries(self) -> list[RepositoryAllowlistEntry]:
        dashboard = {
            item.repo_full_name: item for item in self.store.list_repository_allowlist_entries()
        }
        names = sorted(self.environment_repositories | set(dashboard))
        return [
            RepositoryAllowlistEntry(
                repo_full_name=name,
                source="ENVIRONMENT" if name in self.environment_repositories else "DASHBOARD",
                removable=name not in self.environment_repositories,
                created_at=None if name in self.environment_repositories else dashboard[name].created_at,
            )
            for name in names
        ]

    def is_allowed(self, repository: str) -> bool:
        normalized = normalize_repository_identity(repository)
        return normalized in self.environment_repositories or self.store.get_repository_allowlist_entry(
            normalized
        ) is not None

    def require_allowed(self, repository: str) -> str:
        normalized = normalize_repository_identity(repository)
        if not self.is_allowed(normalized):
            raise RepositoryNotAllowlistedError("Repository is not allowlisted.")
        return normalized

    def add(self, repository: str) -> RepositoryAllowlistEntry:
        normalized = normalize_repository_identity(repository)
        if normalized in self.environment_repositories:
            return RepositoryAllowlistEntry(
                repo_full_name=normalized,
                source="ENVIRONMENT",
                removable=False,
            )
        record = self.store.add_repository_allowlist_entry(
            normalized,
            max_entries=self.max_dashboard_entries,
        )
        if record is None:
            raise RepositoryAllowlistLimitError(
                f"Dashboard repository allowlist is limited to {self.max_dashboard_entries} entries."
            )
        return RepositoryAllowlistEntry(
            repo_full_name=record.repo_full_name,
            source="DASHBOARD",
            removable=True,
            created_at=record.created_at,
        )

    def remove(self, repository: str) -> bool:
        normalized = normalize_repository_identity(repository)
        if normalized in self.environment_repositories:
            raise EnvironmentRepositoryRemovalError(
                "Environment-managed repository entries cannot be removed through the API."
            )
        return self.store.remove_repository_allowlist_entry(normalized)


def normalize_repository_identity(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 2048:
        raise ValueError("Repository identity is required and must not exceed 2048 characters.")
    if "://" in raw:
        return parse_github_repository_url(raw).lower()
    if any(character in raw for character in "\\?#%@:"):
        raise ValueError("Repository identity must use canonical owner/repository form.")
    parts = raw.split("/")
    if len(parts) != 2:
        raise ValueError("Repository identity must contain exactly owner/repository.")
    owner, repository = parts
    _validate_repository_parts(owner, repository)
    return f"{owner}/{repository}".lower()


def parse_github_repository_url(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 2048:
        raise ValueError("Repository URL is required and must not exceed 2048 characters.")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Repository URL is invalid.") from exc
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "github.com":
        raise ValueError("Repository URL must use https://github.com.")
    if parsed.username or parsed.password or port is not None or parsed.query or parsed.fragment:
        raise ValueError("Repository URL must not contain credentials, ports, query, or fragment.")
    if "%" in parsed.path:
        raise ValueError("Repository URL must not contain encoded path characters.")
    if "//" in parsed.path:
        raise ValueError("Repository URL must not contain repeated path separators.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Repository URL must contain exactly owner/repository.")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    _validate_repository_parts(owner, repository)
    return f"{owner}/{repository}"


def _validate_repository_parts(owner: str, repository: str) -> None:
    if not _REPOSITORY_PART_PATTERN.fullmatch(owner) or not _REPOSITORY_PART_PATTERN.fullmatch(
        repository
    ):
        raise ValueError("Repository owner or name is invalid.")
    if owner in {".", ".."} or repository in {".", ".."}:
        raise ValueError("Repository owner or name is invalid.")
