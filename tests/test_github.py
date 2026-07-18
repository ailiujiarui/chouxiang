from pathlib import Path
from subprocess import CompletedProcess

import pytest

from refactor_agent.github import GitHubAutomationError, GitRepositoryManager, canonical_clone_url


def test_canonical_clone_url_accepts_only_repository_identity():
    assert canonical_clone_url("octo/demo") == "https://github.com/octo/demo.git"
    for value in ("https://github.com/octo/demo", "octo/demo/issues", "../demo", "octo/demo.git?x=1"):
        with pytest.raises(GitHubAutomationError):
            canonical_clone_url(value)


def test_repository_manager_only_clones_and_validates_origin(tmp_path: Path):
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path, env):
        commands.append(command)
        if command[:3] == ["git", "remote", "get-url"]:
            return CompletedProcess(command, 0, stdout="https://github.com/octo/demo.git\n", stderr="")
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir(parents=True)
        return CompletedProcess(command, 0, stdout="", stderr="")

    manager = GitRepositoryManager(tmp_path / "checkouts", runner=runner)
    checkout = manager.clone_repository("octo/demo", "main", checkout_label="demo")
    assert checkout.is_dir()
    assert commands[0][:5] == ["git", "clone", "--depth", "1", "--branch"]
    flattened = " ".join(" ".join(command) for command in commands)
    assert " push " not in f" {flattened} "
    assert " commit " not in f" {flattened} "
    assert " checkout -b " not in f" {flattened} "


def test_repository_manager_rejects_noncanonical_origin(tmp_path: Path):
    def runner(command: list[str], cwd: Path, env):
        if command[:3] == ["git", "remote", "get-url"]:
            return CompletedProcess(command, 0, stdout="https://token@github.com/octo/demo.git\n", stderr="")
        Path(command[-1]).mkdir(parents=True)
        return CompletedProcess(command, 0, stdout="", stderr="")

    manager = GitRepositoryManager(tmp_path / "checkouts", runner=runner)
    with pytest.raises(GitHubAutomationError, match="canonical"):
        manager.clone_repository("octo/demo", None)
