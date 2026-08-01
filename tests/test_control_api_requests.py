from __future__ import annotations

import pytest
from pydantic import ValidationError

from refactor_agent.control_api_requests import (
    DashboardUrlJobRequest,
    RepositoryAllowlistRequest,
    SnippetJobRequest,
)
from refactor_agent.webhook import (
    DashboardUrlJobRequest as CompatibleDashboardUrlJobRequest,
)
from refactor_agent.webhook import (
    RepositoryAllowlistRequest as CompatibleRepositoryAllowlistRequest,
)
from refactor_agent.webhook import SnippetJobRequest as CompatibleSnippetJobRequest


def test_control_api_request_models_preserve_fields_and_defaults() -> None:
    url_request = DashboardUrlJobRequest(
        repository_url="https://github.com/octo/demo",
        refactor_request="simplify",
    )
    allowlist_request = RepositoryAllowlistRequest(repository="octo/demo")
    snippet_request = SnippetJobRequest(
        source="x = 1\n",
        refactor_request="review",
    )

    assert url_request.model_dump() == {
        "repository_url": "https://github.com/octo/demo",
        "refactor_request": "simplify",
        "branch": None,
        "target_path": None,
        "tests_path": "tests",
        "persona": "STRICT",
    }
    assert allowlist_request.model_dump() == {"repository": "octo/demo"}
    assert snippet_request.model_dump() == {
        "source": "x = 1\n",
        "refactor_request": "review",
        "tests": None,
        "mode": "REVIEW",
        "persona": "STRICT",
    }


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (DashboardUrlJobRequest, {"repository_url": "url"}),
        (RepositoryAllowlistRequest, {}),
        (SnippetJobRequest, {"source": "x = 1"}),
        (
            DashboardUrlJobRequest,
            {"repository_url": "url", "refactor_request": "work", "persona": "OTHER"},
        ),
        (
            SnippetJobRequest,
            {"source": "x = 1", "refactor_request": "work", "mode": "OTHER"},
        ),
    ],
)
def test_control_api_request_models_preserve_required_and_literal_validation(
    model_type: type[DashboardUrlJobRequest | RepositoryAllowlistRequest | SnippetJobRequest],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


def test_webhook_module_keeps_request_model_compatibility_exports() -> None:
    assert CompatibleDashboardUrlJobRequest is DashboardUrlJobRequest
    assert CompatibleRepositoryAllowlistRequest is RepositoryAllowlistRequest
    assert CompatibleSnippetJobRequest is SnippetJobRequest
