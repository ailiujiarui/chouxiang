"""Pydantic input contracts for control API compatibility routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DashboardUrlJobRequest(BaseModel):
    repository_url: str
    refactor_request: str
    branch: str | None = None
    target_path: str | None = None
    tests_path: str = "tests"
    persona: Literal["STRICT", "TSUNDERE"] = "STRICT"


class RepositoryAllowlistRequest(BaseModel):
    repository: str


class SnippetJobRequest(BaseModel):
    source: str
    refactor_request: str
    tests: str | None = None
    mode: Literal["REVIEW", "VERIFIED_REFACTOR"] = "REVIEW"
    persona: Literal["STRICT", "TSUNDERE"] = "STRICT"


__all__ = [
    "DashboardUrlJobRequest",
    "RepositoryAllowlistRequest",
    "SnippetJobRequest",
]
