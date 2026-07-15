from pathlib import Path
from datetime import datetime, timedelta, timezone

from refactor_agent.config import AppSettings
from refactor_agent.execution_control import ExecutionCancelled, ExecutionControl
from refactor_agent.github import GitHubAutomationService, GitRepositoryManager, _branch_name, canonical_clone_url
from refactor_agent.locator import AUTO_TARGET_PATH
from refactor_agent.llm import MockRefactorClient
from refactor_agent.models import GitHubRefactorJob


class FakeRepoManager:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout
        self.repo_full_name: str | None = None
        self.clone_token: str | None = None
        self.branch_name: str | None = None
        self.pushed: tuple[str, str, str] | None = None

        self.cleaned = False

    def clone_for_issue(self, repo_full_name: str, base_branch: str, issue_number: int, token: str | None = None) -> Path:
        self.repo_full_name = repo_full_name
        self.clone_token = token
        _make_leap_checkout(self.checkout)
        return self.checkout

    def create_branch(self, checkout_path: Path, branch_name: str) -> None:
        self.branch_name = branch_name

    def commit_and_push(self, checkout_path: Path, file_path: str, branch_name: str, message: str, token: str) -> None:
        self.pushed = (file_path, branch_name, message)

    def cleanup(self, checkout_path: Path) -> None:
        self.cleaned = True


class FakeGitHubApi:
    def __init__(self) -> None:
        self.pull_request: dict[str, str] | None = None
        self.issue_comment: dict[str, object] | None = None

    def create_pull_request(self, repo_full_name: str, title: str, head: str, base: str, body: str) -> str:
        self.pull_request = {"repo": repo_full_name, "title": title, "head": head, "base": base, "body": body}
        return "https://github.com/octo/demo/pull/1"

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> None:
        self.issue_comment = {"repo": repo_full_name, "issue": issue_number, "body": body}


def test_github_service_dry_run_refactors_without_push(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    service = GitHubAutomationService(
        settings=AppSettings(
            dry_run=True,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
            retain_checkouts=True,
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
            retain_checkouts=True,
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )
    result = service.process(_job(target_path=AUTO_TARGET_PATH))
    assert result.status == "DRY_RUN"
    assert "return (" in (checkout / "leap_year.py").read_text(encoding="utf-8")


def test_github_service_cleans_checkout_by_default(tmp_path: Path):
    repo_manager = FakeRepoManager(tmp_path / "checkout")
    service = GitHubAutomationService(
        settings=AppSettings(dry_run=True, run_root=tmp_path / ".runs"),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )
    assert service.process(_job()).status == "DRY_RUN"
    assert repo_manager.cleaned is True


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
            retain_checkouts=True,
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        api_client=api,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )
    result = service.process(_job())
    assert result.status == "SUCCESS"
    assert result.job_id == "job-42"
    assert result.pr_url == "https://github.com/octo/demo/pull/1"
    assert repo_manager.repo_full_name == "octo/demo"
    assert repo_manager.clone_token == "secret-token"
    assert repo_manager.pushed is not None
    assert repo_manager.pushed[0] == "leap_year.py"
    assert api.pull_request is not None
    assert "Refactor Agent Report" in api.pull_request["body"]
    assert api.issue_comment is not None
    assert "https://github.com/octo/demo/pull/1" in str(api.issue_comment["body"])


def test_repository_manager_sets_local_bot_identity_before_commit(tmp_path: Path):
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path, env: dict[str, str] | None):
        commands.append(command)
        from subprocess import CompletedProcess

        stdout = " M target.py\n" if command[:3] == ["git", "status", "--porcelain"] else ""
        return CompletedProcess(command, 0, stdout=stdout, stderr="")

    manager = GitRepositoryManager(tmp_path, runner=runner)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    manager.commit_and_push(checkout, "target.py", "refactor-agent/issue-1", "fix issue", "token")
    assert commands[:2] == [
        ["git", "config", "user.name", "Refactor Agent Bot"],
        ["git", "config", "user.email", "refactor-agent@users.noreply.github.com"],
    ]


def test_clone_uses_ephemeral_auth_without_credentials_in_url(tmp_path: Path):
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def runner(command: list[str], cwd: Path, env: dict[str, str] | None):
        from subprocess import CompletedProcess

        calls.append((command, env))
        if command[:4] == ["git", "remote", "get-url", "origin"]:
            return CompletedProcess(command, 0, stdout="https://github.com/octo/demo.git\n", stderr="")
        return CompletedProcess(command, 0, stdout="", stderr="")

    manager = GitRepositoryManager(tmp_path, runner=runner)
    manager.clone_for_issue("octo/demo", "main", 42, token="secret-token")
    clone_command, clone_env = calls[0]
    assert clone_command[0:2] == ["git", "clone"]
    assert clone_command[-2] == canonical_clone_url("octo/demo")
    assert "secret-token" not in " ".join(clone_command)
    assert clone_env is not None
    assert clone_env["GIT_ASKPASS_PASSWORD"] == "secret-token"
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert clone_env["GIT_CONFIG_VALUE_0"] == ""
    assert clone_env["GIT_CONFIG_KEY_1"] == "credential.useHttpPath"
    assert clone_env["GIT_CONFIG_VALUE_1"] == "true"
    assert not Path(clone_env["GIT_ASKPASS"]).exists()


def test_job_branch_name_is_deterministic():
    assert _branch_name(42, "job-42") == _branch_name(42, "job-42")
    assert _branch_name(42, "job-42") != _branch_name(42, "job-43")


class StageControl:
    def __init__(self, cancel_at: str | None = None) -> None:
        self.stages: list[str] = []
        self.cancel_at = cancel_at

    def checkpoint(self, stage: str) -> None:
        self.stages.append(stage)
        if stage == self.cancel_at:
            raise ExecutionCancelled(stage)

    def bounded_timeout(self, timeout: float, stage: str) -> float:
        self.checkpoint(f"before-{stage}")
        return timeout


def test_github_service_checkpoints_irreversible_side_effects(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    api = FakeGitHubApi()
    control = StageControl()
    service = GitHubAutomationService(
        settings=AppSettings(
            github_token="secret-token",
            dry_run=False,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
            retain_checkouts=True,
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        api_client=api,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )

    assert service.process(_job(), execution_control=control).status == "SUCCESS"  # type: ignore[arg-type]
    assert [
        "before-clone",
        "before-create-branch",
        "before-write-candidate",
        "before-push",
        "before-create-pull-request",
        "before-create-issue-comment",
    ] == [stage for stage in control.stages if stage in {
        "before-clone",
        "before-create-branch",
        "before-write-candidate",
        "before-push",
        "before-create-pull-request",
        "before-create-issue-comment",
    }]


def test_github_service_reports_manual_cleanup_when_cancelled_after_push(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    api = FakeGitHubApi()
    control = StageControl(cancel_at="before-create-pull-request")
    service = GitHubAutomationService(
        settings=AppSettings(
            github_token="secret-token",
            dry_run=False,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
            retain_checkouts=True,
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        api_client=api,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )

    result = service.process(_job(), execution_control=control)  # type: ignore[arg-type]

    assert repo_manager.pushed is not None
    assert api.pull_request is None
    assert result.status == "FAILED"
    assert result.requires_manual_cleanup is True
    assert "manual cleanup" in (result.error or "").lower()


def test_github_service_retains_pr_url_when_cancelled_after_pr_creation(tmp_path: Path):
    checkout = tmp_path / "checkout"
    repo_manager = FakeRepoManager(checkout)
    api = FakeGitHubApi()
    control = StageControl(cancel_at="after-create-pull-request")
    service = GitHubAutomationService(
        settings=AppSettings(
            github_token="secret-token",
            dry_run=False,
            run_root=tmp_path / ".runs",
            database_path=tmp_path / ".runs" / "runs.sqlite",
            retain_checkouts=True,
        ),
        repo_manager=repo_manager,  # type: ignore[arg-type]
        api_client=api,  # type: ignore[arg-type]
        llm_factory=lambda: MockRefactorClient(),
    )

    result = service.process(_job(), execution_control=control)  # type: ignore[arg-type]

    assert result.status == "FAILED"
    assert result.requires_manual_cleanup is True
    assert result.pr_url == "https://github.com/octo/demo/pull/1"
    assert api.issue_comment is None


def test_github_http_timeout_uses_remaining_deadline(monkeypatch):
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    control = ExecutionControl(deadline_at=now + timedelta(seconds=8), clock=lambda: now)
    captured: dict[str, float] = {}

    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {"html_url": "https://github.com/octo/demo/pull/1"}

        text = ""

    def fake_post(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return Response()

    monkeypatch.setattr("refactor_agent.github.httpx.post", fake_post)
    from refactor_agent.github import GitHubApiClient

    client = GitHubApiClient("token", execution_control=control)
    client.create_pull_request("octo/demo", "title", "head", "main", "body")

    assert captured["timeout"] == 8


def _job(target_path: str = "leap_year.py") -> GitHubRefactorJob:
    return GitHubRefactorJob(
        job_id="job-42",
        delivery_id="delivery-42",
        repo_full_name="octo/demo",
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
