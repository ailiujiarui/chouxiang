from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from refactor_agent.benchmark import (
    BenchmarkObservation,
    render_benchmark_markdown,
    render_manifest_benchmark_markdown,
    run_benchmark,
    run_manifest_benchmark,
    serialize_benchmark,
    serialize_manifest_benchmark,
)
from refactor_agent.models import BenchmarkCaseRecord, BenchmarkRunRecord
from refactor_agent.store import SQLiteRunStore


class BuiltinBenchmarkRunner(Protocol):
    def __call__(
        self,
        *,
        run_root: Path,
        sandbox_backend: str,
        graph_backend: str,
        timeout_seconds: float,
    ) -> list[BenchmarkObservation]: ...


class ManifestBenchmarkRunner(Protocol):
    def __call__(
        self,
        *,
        manifest_path: Path,
        provider: str,
        run_root: Path,
        cache_root: Path,
        database_path: Path,
        case_names: set[str],
        timeout_seconds: float,
    ) -> tuple[BenchmarkRunRecord, list[BenchmarkCaseRecord]]: ...


class BenchmarkCaseReader(Protocol):
    def list_benchmark_case_results(self, run_id: str) -> list[BenchmarkCaseRecord]: ...


BenchmarkStoreFactory = Callable[[Path], BenchmarkCaseReader]


@dataclass(frozen=True)
class BenchmarkExecutionResult:
    mode: Literal["BUILTIN", "MANIFEST"]
    markdown: str
    json_path: Path
    markdown_path: Path
    exit_code: int


def execute_benchmark(
    *,
    output_dir: Path,
    run_root: Path,
    timeout_seconds: float,
    deadline_seconds: int,
    sandbox_backend: str,
    graph_backend: str,
    manifest_path: Path | None,
    provider: str,
    compare_run_id: str | None,
    case_names: Sequence[str] | None,
    database_path: Path | None,
    cache_root: Path,
    run_builtin: BuiltinBenchmarkRunner = run_benchmark,
    run_manifest: ManifestBenchmarkRunner = run_manifest_benchmark,
    store_factory: BenchmarkStoreFactory = SQLiteRunStore,
) -> BenchmarkExecutionResult:
    """Execute a benchmark and write its JSON/Markdown evidence artifacts."""

    if manifest_path is not None:
        if database_path is None:
            raise ValueError("database_path is required for manifest benchmarks")
        run_record, case_records = run_manifest(
            manifest_path=manifest_path,
            provider=provider,
            run_root=run_root,
            cache_root=cache_root,
            database_path=database_path,
            case_names=set(case_names or []),
            timeout_seconds=min(timeout_seconds, float(deadline_seconds)),
        )
        store = store_factory(database_path)
        previous = (
            store.list_benchmark_case_results(compare_run_id)
            if compare_run_id
            else None
        )
        markdown = render_manifest_benchmark_markdown(run_record, case_records, previous)
        serialized = serialize_manifest_benchmark(run_record, case_records)
        exit_code = 0 if run_record.status == "SUCCESS" else 1
        mode: Literal["BUILTIN", "MANIFEST"] = "MANIFEST"
    else:
        observations = run_builtin(
            run_root=run_root,
            sandbox_backend=sandbox_backend,
            graph_backend=graph_backend,
            timeout_seconds=timeout_seconds,
        )
        markdown = render_benchmark_markdown(observations)
        serialized = serialize_benchmark(observations)
        exit_code = 0
        mode = "BUILTIN"

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "benchmark.json"
    markdown_path = output_dir / "benchmark.md"
    json_path.write_text(serialized + "\n", encoding="utf-8")
    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    return BenchmarkExecutionResult(
        mode=mode,
        markdown=markdown,
        json_path=json_path,
        markdown_path=markdown_path,
        exit_code=exit_code,
    )
