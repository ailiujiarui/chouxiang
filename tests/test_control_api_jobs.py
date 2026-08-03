from __future__ import annotations

import pytest

from refactor_agent.control_api import normalize_git_ref as PublicNormalizeGitRef
from refactor_agent.control_api import normalize_repo_path as PublicNormalizeRepoPath
from refactor_agent.control_api_jobs import (
    build_dashboard_job_id,
    normalize_git_ref,
    normalize_repo_path,
    prepare_analysis_job,
    prepare_dashboard_url_job,
    prepare_snippet_job,
)
from refactor_agent.control_api_requests import DashboardUrlJobRequest, SnippetJobRequest
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.models import AnalysisRequest, EvidenceLevel, RepositoryJobKind
from refactor_agent.repository_allowlist import RepositoryNotAllowlistedError
from refactor_agent.webhook import build_dashboard_job_id as CompatibleBuildDashboardJobId


def test_prepare_dashboard_url_job_normalizes_and_preserves_legacy_job_shape() -> None:
    job = prepare_dashboard_url_job(
        DashboardUrlJobRequest(
            repository_url="https://github.com/octo/demo.git",
            refactor_request="  simplify this module  ",
            branch=" feature/demo ",
            target_path="src\\app.py",
            tests_path="tests\\unit",
            persona="TSUNDERE",
        ),
        _AllowlistPolicy(),
    )

    assert job.job_kind == RepositoryJobKind.DASHBOARD_URL
    assert job.job_id.startswith("octo__demo__url__")
    assert job.delivery_id.startswith("dashboard:")
    assert job.repo_full_name == "octo/demo"
    assert job.default_branch == "feature/demo"
    assert job.issue_text == "simplify this module"
    assert job.target_path == "src/app.py"
    assert job.tests_path == "tests/unit"
    assert job.event_name == "dashboard_url"
    assert job.persona == "TSUNDERE"


def test_prepare_snippet_job_preserves_validation_order_and_payload_shape() -> None:
    job = prepare_snippet_job(
        SnippetJobRequest(
            source="  x = 1  ",
            tests="  def test_x():\n    assert x == 1  ",
            refactor_request="  review  ",
            mode="VERIFIED_REFACTOR",
        )
    )

    assert job.job_kind == RepositoryJobKind.SNIPPET
    assert job.repo_full_name == "local/snippet"
    assert job.issue_text == "review"
    assert job.snippet_source == "x = 1\n"
    assert job.snippet_tests == "def test_x():\n    assert x == 1\n"
    assert job.snippet_mode == "VERIFIED_REFACTOR"
    assert job.event_name == "snippet"

    with pytest.raises(ValueError, match="Refactor request"):
        prepare_snippet_job(
            SnippetJobRequest(
                source="def broken(",
                refactor_request="   ",
            )
        )
    with pytest.raises(ValueError, match="requires pytest"):
        prepare_snippet_job(
            SnippetJobRequest(
                source="x = 1",
                refactor_request="review",
                mode="VERIFIED_REFACTOR",
            )
        )


def test_prepare_analysis_job_declares_evidence_for_snippet_and_repository() -> None:
    snippet = prepare_analysis_job(
        AnalysisRequest(
            input_kind="SNIPPET",
            instruction="  analyze  ",
            source="  x = 1  ",
        ),
        _AllowlistPolicy(),
    )
    repository = prepare_analysis_job(
        AnalysisRequest(
            input_kind="REPOSITORY_URL",
            instruction="  analyze repository  ",
            repository_url="https://github.com/octo/demo",
            ref=" main ",
        ),
        _AllowlistPolicy(),
    )

    assert snippet.evidence_level == EvidenceLevel.STATIC
    assert snippet.job.event_name == "analysis"
    assert snippet.job.snippet_mode == "REVIEW"
    assert snippet.job.snippet_source == "x = 1\n"
    assert repository.evidence_level == EvidenceLevel.REPOSITORY_TESTS
    assert repository.job.target_path == AUTO_TARGET_PATH
    assert repository.job.default_branch == "main"
    assert repository.job.issue_text == "analyze repository"


def test_prepare_jobs_propagates_allowlist_denial_without_http_dependency() -> None:
    with pytest.raises(RepositoryNotAllowlistedError, match="not allowlisted"):
        prepare_dashboard_url_job(
            DashboardUrlJobRequest(
                repository_url="https://github.com/octo/blocked",
                refactor_request="review",
            ),
            _AllowlistPolicy(),
        )


def test_job_helper_compatibility_exports_are_stable() -> None:
    assert PublicNormalizeGitRef is normalize_git_ref
    assert PublicNormalizeRepoPath is normalize_repo_path
    assert CompatibleBuildDashboardJobId is build_dashboard_job_id


class _AllowlistPolicy:
    def require_allowed(self, repo_full_name: str) -> str:
        if repo_full_name != "octo/demo":
            raise RepositoryNotAllowlistedError("Repository is not allowlisted.")
        return repo_full_name
