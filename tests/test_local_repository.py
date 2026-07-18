from pathlib import Path

from refactor_agent.config import AppSettings
from refactor_agent.llm import MockRefactorClient
from refactor_agent.local_repository import LocalRepositoryRefactorService
from refactor_agent.models import GitHubRefactorJob, RepositoryJobKind


class FakeReadOnlyRepoManager:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout
        self.clone_call: tuple[str, str | None, str | None, str] | None = None
        self.cleaned = False

    def clone_repository(
        self,
        repo_full_name: str,
        ref: str | None,
        token: str | None,
        checkout_label: str,
    ) -> Path:
        self.clone_call = (repo_full_name, ref, token, checkout_label)
        _make_checkout(self.checkout)
        return self.checkout

    def cleanup(self, checkout_path: Path) -> None:
        self.cleaned = True


def test_local_repository_service_never_writes_back_or_publishes(tmp_path: Path):
    checkout = tmp_path / "checkout"
    manager = FakeReadOnlyRepoManager(checkout)
    settings = AppSettings(
        dry_run=False,
        github_token="read-token",
        allowed_repositories={"octo/demo"},
        run_root=tmp_path / ".runs",
        database_path=tmp_path / ".runs" / "runs.sqlite",
        retain_checkouts=True,
    )
    service = LocalRepositoryRefactorService(
        settings,
        repo_manager=manager,
        llm_factory=lambda: MockRefactorClient(),
    )

    result = service.process(_url_job())

    assert result.status == "DRY_RUN"
    assert result.branch_name is None
    assert result.pr_url is None
    assert result.run_id is not None
    assert manager.clone_call == ("octo/demo", None, "read-token", "url-job-1")
    assert "if year % 4" in (checkout / "leap_year.py").read_text(encoding="utf-8")
    candidate = settings.run_root / result.run_id / "artifacts" / "candidate.py"
    assert candidate.is_file()
    assert "return (" in candidate.read_text(encoding="utf-8")


def test_local_repository_service_cleans_checkout_by_default(tmp_path: Path):
    manager = FakeReadOnlyRepoManager(tmp_path / "checkout")
    service = LocalRepositoryRefactorService(
        AppSettings(
            run_root=tmp_path / ".runs",
            mock_llm=True,
            allowed_repositories={"octo/demo"},
        ),
        repo_manager=manager,
    )

    assert service.process(_url_job()).status == "DRY_RUN"
    assert manager.cleaned is True


def test_local_repository_service_rechecks_repository_allowlist(tmp_path: Path):
    manager = FakeReadOnlyRepoManager(tmp_path / "checkout")
    service = LocalRepositoryRefactorService(
        AppSettings(run_root=tmp_path / ".runs", mock_llm=True),
        repo_manager=manager,
    )

    result = service.process(_url_job())

    assert result.status == "FAILED"
    assert "allowlist" in (result.error or "").lower()
    assert manager.clone_call is None


def _url_job() -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_kind=RepositoryJobKind.DASHBOARD_URL,
        job_id="url-job-1",
        delivery_id="dashboard:delivery-1",
        repo_full_name="octo/demo",
        default_branch=None,
        issue_number=None,
        issue_title="Dashboard URL 本地简化任务",
        issue_text="简化 leap_year.py 中的 is_leap_year 函数并保持行为不变",
        target_path="leap_year.py",
        tests_path="tests",
        event_name="dashboard_url",
        action="submitted",
    )


def _make_checkout(checkout: Path) -> None:
    tests = checkout / "tests"
    tests.mkdir(parents=True)
    (checkout / "leap_year.py").write_text(
        "def is_leap_year(year):\n"
        "    if year % 4 == 0:\n"
        "        if year % 100 == 0:\n"
        "            return True\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    (tests / "test_leap_year.py").write_text(
        "from leap_year import is_leap_year\n\n\n"
        "def test_leap_year_rules():\n"
        "    assert is_leap_year(2000) is True\n"
        "    assert is_leap_year(2024) is True\n"
        "    assert is_leap_year(1900) is False\n"
        "    assert is_leap_year(2023) is False\n",
        encoding="utf-8",
    )
