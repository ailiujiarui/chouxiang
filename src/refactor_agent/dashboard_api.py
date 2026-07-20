from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class DashboardApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class DashboardApiClient:
    def __init__(
        self,
        api_url: str,
        admin_token: str | None = None,
        timeout_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.admin_token = admin_token
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._request("GET", "/jobs", params={"limit": limit}).get("jobs", []))

    def get_capabilities(self) -> dict[str, Any]:
        return dict(self._request("GET", "/capabilities"))

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")

    def list_events(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"/jobs/{job_id}/events").get("events", []))

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._request("GET", "/runs", params={"limit": limit}).get("runs", []))

    def get_trajectory(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"/runs/{run_id}/trajectory").get("trajectory", []))

    def get_artifact(self, run_id: str, artifact_name: str) -> str:
        return str(self._request("GET", f"/runs/{run_id}/artifacts/{artifact_name}", expect_json=False))

    def list_benchmarks(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._request("GET", "/benchmarks", params={"limit": limit}).get("runs", []))

    def get_benchmark(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/benchmarks/{run_id}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._control("POST", f"/jobs/{job_id}/cancel")

    def retry_job(self, job_id: str) -> dict[str, Any]:
        return self._control("POST", f"/jobs/{job_id}/retry")

    def list_repository_allowlist(self) -> list[dict[str, Any]]:
        return list(self._control("GET", "/admin/repository-allowlist").get("entries", []))

    def add_repository_allowlist(self, repository: str) -> dict[str, Any]:
        return self._control(
            "POST",
            "/admin/repository-allowlist",
            json={"repository": repository},
        )

    def remove_repository_allowlist(self, repo_full_name: str) -> dict[str, Any]:
        owner, repository = repo_full_name.split("/", 1)
        path = f"/admin/repository-allowlist/{quote(owner, safe='')}/{quote(repository, safe='')}"
        return self._control("DELETE", path)

    def submit_url_job(
        self,
        *,
        repository_url: str,
        refactor_request: str,
        branch: str | None,
        target_path: str | None,
        tests_path: str,
        persona: str = "STRICT",
    ) -> dict[str, Any]:
        return self._control(
            "POST",
            "/jobs/url",
            json={
                "repository_url": repository_url,
                "refactor_request": refactor_request,
                "branch": branch,
                "target_path": target_path,
                "tests_path": tests_path,
                "persona": persona,
            },
        )

    def submit_snippet_job(
        self,
        *,
        source: str,
        refactor_request: str,
        tests: str | None,
        mode: str,
        persona: str,
    ) -> dict[str, Any]:
        return self._control(
            "POST",
            "/jobs/snippet",
            json={
                "source": source,
                "refactor_request": refactor_request,
                "tests": tests,
                "mode": mode,
                "persona": persona,
            },
        )

    def submit_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._control("POST", "/analysis", json=payload)

    def _control(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = (
            {"Authorization": f"Bearer {self.admin_token}"}
            if self.admin_token
            else None
        )
        return self._request(
            method,
            path,
            headers=headers,
            json=json,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
    ):
        try:
            with httpx.Client(
                base_url=self.api_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.request(method, path, params=params, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise DashboardApiError(f"Dashboard API request failed: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise DashboardApiError(str(detail), response.status_code)
        return response.json() if expect_json else response.text
