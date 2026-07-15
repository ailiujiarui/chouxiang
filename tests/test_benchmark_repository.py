from pathlib import Path
from subprocess import CompletedProcess

from refactor_agent.benchmark_manifest import load_manifest
from refactor_agent.benchmark_repository import BenchmarkRepositoryCache


def test_repository_cache_uses_anonymous_canonical_origin_and_exact_commit(tmp_path: Path):
    case = load_manifest(Path("benchmarks/manifest.toml")).cases[0]
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout: float):
        commands.append(command)
        if command[:3] == ["git", "config", "--get"]:
            return CompletedProcess(command, 0, stdout="https://github.com/more-itertools/more-itertools.git\n", stderr="")
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return CompletedProcess(command, 0, stdout="", stderr="")

    cache = BenchmarkRepositoryCache(tmp_path / "cache", runner=runner)
    destination = tmp_path / "checkout"
    cache.prepare(case, destination)

    flattened = [" ".join(command) for command in commands]
    assert any("https://github.com/more-itertools/more-itertools.git" in item for item in flattened)
    assert all("@" not in item and "token" not in item.lower() for item in flattened)
    assert any(case.commit in item and "checkout --detach" in item for item in flattened)
    assert destination.is_dir()


def test_repository_cache_reuses_existing_bare_clone(tmp_path: Path):
    case = load_manifest(Path("benchmarks/manifest.toml")).cases[0]
    bare = tmp_path / "cache" / "more-itertools" / "more-itertools.git"
    bare.mkdir(parents=True)
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout: float):
        commands.append(command)
        if command[:3] == ["git", "config", "--get"]:
            return CompletedProcess(command, 0, stdout="https://github.com/more-itertools/more-itertools.git\n", stderr="")
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return CompletedProcess(command, 0, stdout="", stderr="")

    BenchmarkRepositoryCache(tmp_path / "cache", runner=runner).prepare(case, tmp_path / "checkout")

    assert not any(command[:3] == ["git", "clone", "--bare"] for command in commands)
    assert any(command[:3] == ["git", "fetch", "origin"] for command in commands)


def test_default_repository_runner_drops_credentials_and_disables_helpers(tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GIT_ASKPASS_PASSWORD", "secret")

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("refactor_agent.benchmark_repository.subprocess.run", fake_run)

    BenchmarkRepositoryCache(tmp_path / "cache")._run(["git", "version"], tmp_path, 5)

    env = captured["env"]
    assert "GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS_PASSWORD" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
