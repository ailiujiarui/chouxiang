from __future__ import annotations

import json
from pathlib import Path

import pytest

from refactor_agent.benchmark import BenchmarkObservation
from refactor_agent.benchmark_service import execute_benchmark
from refactor_agent.models import BenchmarkCaseRecord, BenchmarkRunRecord


def test_builtin_benchmark_service_runs_and_writes_evidence(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    observation = _observation()

    def run_builtin(**options):
        captured.update(options)
        return [observation]

    execution = execute_benchmark(
        output_dir=tmp_path / "evidence",
        run_root=tmp_path / "runs",
        timeout_seconds=12.5,
        deadline_seconds=900,
        sandbox_backend="docker",
        graph_backend="loop",
        manifest_path=None,
        provider="mock",
        compare_run_id=None,
        case_names=None,
        database_path=None,
        cache_root=tmp_path / "cache",
        run_builtin=run_builtin,
    )

    assert execution.mode == "BUILTIN"
    assert execution.exit_code == 0
    assert captured == {
        "run_root": tmp_path / "runs",
        "sandbox_backend": "docker",
        "graph_backend": "loop",
        "timeout_seconds": 12.5,
    }
    assert json.loads(execution.json_path.read_text(encoding="utf-8"))["sample_count"] == 1
    assert execution.markdown_path.read_text(encoding="utf-8") == execution.markdown + "\n"


def test_manifest_benchmark_service_uses_comparison_and_bounded_timeout(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    run_record = BenchmarkRunRecord(
        run_id="benchmark-current",
        manifest_hash="manifest-hash",
        provider="deepseek",
        model="deepseek-chat",
        status="FAILED",
        generated_at="2026-08-02T00:00:00Z",
    )
    case_record = _case_record("benchmark-current")
    previous_record = _case_record("benchmark-previous")

    def run_manifest(**options):
        captured.update(options)
        return run_record, [case_record]

    class Store:
        def list_benchmark_case_results(self, run_id: str):
            captured["compare_run_id"] = run_id
            return [previous_record]

    execution = execute_benchmark(
        output_dir=tmp_path / "evidence",
        run_root=tmp_path / "runs",
        timeout_seconds=60,
        deadline_seconds=45,
        sandbox_backend="subprocess",
        graph_backend="langgraph",
        manifest_path=tmp_path / "manifest.json",
        provider="deepseek",
        compare_run_id="benchmark-previous",
        case_names=["case-one"],
        database_path=tmp_path / "benchmark.sqlite",
        cache_root=tmp_path / "cache",
        run_manifest=run_manifest,
        store_factory=lambda path: Store(),
    )

    assert execution.mode == "MANIFEST"
    assert execution.exit_code == 1
    assert captured["case_names"] == {"case-one"}
    assert captured["timeout_seconds"] == 45.0
    assert captured["compare_run_id"] == "benchmark-previous"
    assert "## Comparison" in execution.markdown
    assert json.loads(execution.json_path.read_text(encoding="utf-8"))["run"]["status"] == "FAILED"


def test_manifest_benchmark_requires_resolved_database_path(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="database_path is required for manifest benchmarks",
    ):
        execute_benchmark(
            output_dir=tmp_path / "evidence",
            run_root=tmp_path / "runs",
            timeout_seconds=30,
            deadline_seconds=900,
            sandbox_backend="subprocess",
            graph_backend="langgraph",
            manifest_path=tmp_path / "manifest.json",
            provider="mock",
            compare_run_id=None,
            case_names=None,
            database_path=None,
            cache_root=tmp_path / "cache",
        )


def _observation() -> BenchmarkObservation:
    return BenchmarkObservation(
        case="simple",
        tag="simple-function",
        status="SUCCESS",
        attempts=1,
        loc_before=5,
        loc_after=2,
        cc_before=3,
        cc_after=1,
        mutation_kill_rate=1.0,
        adversarial_passed=True,
        runtime_seconds=1.25,
        reward=8.0,
    )


def _case_record(run_id: str) -> BenchmarkCaseRecord:
    return BenchmarkCaseRecord(
        run_id=run_id,
        case_name="case-one",
        repository="owner/repository",
        commit="abc123",
        provider="deepseek",
        model="deepseek-chat",
        status="SUCCESS",
        expected_status="SUCCESS",
        normalized_hash="a" * 64,
    )
