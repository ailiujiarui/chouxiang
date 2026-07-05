from pathlib import Path

from refactor_agent.config import AppSettings
from refactor_agent.github import GitHubAutomationService
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.llm import MockRefactorClient
from refactor_agent.models import GitHubRefactorJob


class FakeRepoManager:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout
        self.clone_url: str | None = None
        self.branch_name: str | None = None
        self.pushed: tuple[str, str, str] | None = None

    def clone_for_issue(self, repo_full_name: str, clone_url: str, base_branch: str, issue_number: int) -> Path:
        self.clone_url = clone_url
        _make_leap_checkout(self.checkout)
        return self.checkout

    def create_branch(self, checkout_path: Path, branch_name: str) -> None:
        self.branch_name = branch_name

    def commit_and_push(self, checkout_path: Path, file_path: str, branch_name: str, message: str) -> None:
        self.pushed = (file_path, branch_name, message)


class FakeGitHubApi:
    def __init__(self) -> None:
        self.pull_request: dict[str, str] | None = None

    def create_pull_request(self, repo_full_name: str, title: str, head: str, base: str, body: str) -> str:
        self.pull_request = {"repo": repo_full_name, "title": title, "head": head, "base": base, "body": body}
        return "https://github.com/octo/demo/pull/1"

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> None:
        raise AssertionError("unexpected failure comment")


def test_github_service_dry_run_refactors_without_push(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    service = GitHubAutomationService(
        settings=AppSettings(
            dry_run=True,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )
    result = service.process(_job())
    assert result.status == "DRY_RUN"
    assert result.job_id == "job-42"
    assert repo_manager.pushed is None
    assert "return (" in (checkout / "leap_year.py").read_text(encoding="utf-8")


def test_github_service_auto_locates_target_file(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    service = GitHubAutomationService(
        settings=AppSettings(
            dry_run=True,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )
    result = service.process(_job(target_path=AUTO_TARGET_PATH))
    assert result.status == "DRY_RUN"
    assert "return (" in (checkout / "leap_year.py").read_text(encoding="utf-8")


def test_github_service_pushes_and_creates_pr(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    api = FakeGitHubApi()
    service = GitHubAutomationService(
        settings=AppSettings(
            github_token="secret-token",
            dry_run=False,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        api_client=api,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )
    result = service.process(_job())
    assert result.status == "SUCCESS"
    assert result.job_id == "job-42"
    assert result.pr_url == "https://github.com/octo/demo/pull/1"
    assert repo_manager.clone_url is not None
    assert "x-access-token:" in repo_manager.clone_url
    assert repo_manager.pushed is not None
    assert repo_manager.pushed[0] == "leap_year.py"
    assert api.pull_request is not None
    assert "Refactor Agent Report" in api.pull_request["body"]


def _job(target_path: str = "leap_year.py") -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_id="job-42",
        repo_full_name="octo/demo",
        clone_url="https://github.com/octo/demo.git",
        default_branch="main",
        issue_number=42,
        issue_title="Leap year bug",
        issue_text="target: leap_year.py\ntests: tests\n1900 should not be a leap year",
        target_path=target_path,
        tests_path="tests",
        event_name="issues",
        action="opened",
    )


def _make_leap_checkout(checkout: Path) -> None:
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
