import json
from typer.testing import CliRunner

from refactor_agent.benchmark import (
    BENCHMARK_CASES,
    BenchmarkObservation,
    render_benchmark_markdown,
    serialize_benchmark,
)
from refactor_agent.benchmark_runner import normalized_result_hash
from refactor_agent.cli import app


runner = CliRunner()


def test_benchmark_suite_covers_required_scenarios():
    tags = {case.tag for case in BENCHMARK_CASES}
    assert len(BENCHMARK_CASES) >= 6
    assert {
        "simple-function",
        "low-complexity-target",
        "class-method",
        "module-statement",
        "adversarial-weak-tests",
        "unsafe-rejection",
    } <= tags


def test_benchmark_serialization_and_markdown_are_reproducible():
    observations = [
        BenchmarkObservation(
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
    ]

    payload = serialize_benchmark(observations, generated_at="2026-07-13T00:00:00Z")
    assert json.loads(payload)["sample_count"] == 1
    assert json.loads(payload)["cases"][0]["mutation_kill_rate"] == 1.0

    markdown = render_benchmark_markdown(observations)
    assert "Sample count: 1" in markdown
    assert "| simple | simple-function | SUCCESS | 1 | 5 -> 2 | 3 -> 1 | 100.0% | pass | 8.00 |" in markdown


def test_benchmark_cli_writes_json_and_markdown(tmp_path, monkeypatch):
    observation = BenchmarkObservation(
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
    monkeypatch.setattr("refactor_agent.cli.run_benchmark", lambda **kwargs: [observation])

    result = runner.invoke(app, ["benchmark", "--output-dir", str(tmp_path / "evidence")])

    assert result.exit_code == 0
    assert (tmp_path / "evidence" / "benchmark.json").is_file()
    assert (tmp_path / "evidence" / "benchmark.md").is_file()


def test_normalized_external_result_hash_ignores_timestamps_and_durations():
    first = {
        "case_name": "case",
        "status": "SUCCESS",
        "runtime_seconds": 1.0,
        "generated_at": "2026-07-14T00:00:00Z",
    }
    second = {
        **first,
        "runtime_seconds": 99.0,
        "generated_at": "2026-07-15T00:00:00Z",
    }

    assert normalized_result_hash(first) == normalized_result_hash(second)


def test_normalized_external_result_hash_ignores_run_specific_error_paths():
    first = {"case_name": "case", "status": "FAILED", "failure_category": "PYTEST", "error": "C:/run/one"}
    second = {**first, "error": "D:/run/two"}

    assert normalized_result_hash(first) == normalized_result_hash(second)


def test_benchmark_cli_exposes_manifest_provider_and_compare_options():
    result = runner.invoke(app, ["benchmark", "--help"])

    assert result.exit_code == 0
    assert "--manifest" in result.stdout
    assert "--provider" in result.stdout
    assert "--compare" in result.stdout
