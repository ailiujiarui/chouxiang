from pathlib import Path

import pytest

from refactor_agent.benchmark_runner import (
    ExternalBenchmarkRunner,
    apply_gold_snapshot,
    build_benchmark_setup_command,
)
from refactor_agent.benchmark_manifest import BenchmarkCase
from refactor_agent.llm import LLMError


def test_external_benchmark_rejects_subprocess_backend(tmp_path: Path):
    with pytest.raises(ValueError, match="Docker"):
        ExternalBenchmarkRunner(
            run_root=tmp_path / "runs",
            cache_root=tmp_path / "cache",
            sandbox_backend="subprocess",
        )


def test_benchmark_setup_command_is_offline_read_only_and_credential_free(tmp_path: Path):
    command = build_benchmark_setup_command(
        checkout=tmp_path / "checkout",
        docker_image="refactor-agent-benchmark:py312",
        memory="512m",
        cpus=1.0,
    )
    flattened = " ".join(command)

    assert command[:3] == ["docker", "run", "--rm"]
    assert "--network none" in flattened
    assert ":/workspace:ro" in flattened
    assert "--read-only" in command
    assert "--cap-drop ALL" in flattened
    assert "no-new-privileges" in flattened
    assert "pip install --no-deps -e /tmp/repository" in flattened
    assert "GITHUB_TOKEN" not in flattened
    assert "DEEPSEEK_API_KEY" not in flattened


def test_apply_gold_snapshot_replaces_only_matching_method():
    source = (
        "class Values:\n"
        "    def contains(self, value):\n"
        "        return False\n\n"
        "def untouched():\n"
        "    return 'same'\n"
    )
    gold = (
        "class Values:\n"
        "    def contains(self, value):\n"
        "        return value in self.items\n"
    )

    result = apply_gold_snapshot(source, gold)

    assert "return value in self.items" in result
    assert "def untouched():\n    return 'same'" in result


def test_benchmark_lock_pins_pytest_with_hash():
    lock = Path("benchmarks/requirements.lock").read_text(encoding="utf-8")
    assert "pytest==9.1.1" in lock
    assert lock.count("--hash=sha256:") >= 6


def test_external_benchmark_classifies_provider_initialization_failure(tmp_path: Path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.write_text("", encoding="utf-8")
    case = BenchmarkCase(
        name="provider-failure",
        category="provider",
        repository="octo/demo",
        commit="a" * 40,
        target="value.py",
        tests="tests",
        issue="fix value",
        expected_status="SUCCESS",
        seed_patch=fixture,
        gold_snapshot=fixture,
        docker_test_command=("python", "-m", "pytest", "tests"),
    )

    class Cache:
        def prepare(self, case, destination):
            destination.mkdir(parents=True)
            (destination / "value.py").write_text("def value():\n    return 0\n", encoding="utf-8")
            (destination / "tests").mkdir()

    runner = ExternalBenchmarkRunner(
        run_root=tmp_path / "runs",
        cache_root=tmp_path / "cache",
        repository_cache=Cache(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr("refactor_agent.benchmark_runner._run_checked", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_client",
        lambda *args, **kwargs: (_ for _ in ()).throw(LLMError("provider unavailable")),
    )

    result = runner.run_case(case, provider="deepseek")

    assert result.failure_category == "PROVIDER"
