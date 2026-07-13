import json
from typer.testing import CliRunner

from refactor_agent.benchmark import (
    BENCHMARK_CASES,
    BenchmarkObservation,
    render_benchmark_markdown,
    serialize_benchmark,
)
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
