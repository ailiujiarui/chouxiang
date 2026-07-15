from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel

from refactor_agent.benchmark_manifest import BenchmarkCase
from refactor_agent.benchmark_repository import BenchmarkRepositoryCache
from refactor_agent.llm import DeepSeekClient, LLMError, RefactorClient
from refactor_agent.metrics import analyze_file
from refactor_agent.models import LLMRefactorResult, LLMUsage, MetricsSnapshot, RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


class BenchmarkFailureCategory(StrEnum):
    TARGETING = "TARGETING"
    AST_GUARD = "AST_GUARD"
    PYTEST = "PYTEST"
    ADVERSARY = "ADVERSARY"
    MUTATION = "MUTATION"
    TIMEOUT = "TIMEOUT"
    PROVIDER = "PROVIDER"
    INFRASTRUCTURE = "INFRASTRUCTURE"


class BenchmarkCaseResult(BaseModel):
    case_name: str
    repository: str
    commit: str
    provider: str
    model: str
    status: str
    expected_status: str
    failure_category: BenchmarkFailureCategory | None = None
    attempts: int = 0
    loc_before: int | None = None
    loc_after: int | None = None
    cc_before: int | None = None
    cc_after: int | None = None
    mutation_kill_rate: float | None = None
    adversarial_passed: bool | None = None
    runtime_seconds: float = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0
    normalized_hash: str
    error: str | None = None


class ExternalBenchmarkRunner:
    def __init__(
        self,
        run_root: Path,
        cache_root: Path,
        database_path: Path | None = None,
        sandbox_backend: str = "docker",
        docker_image: str = "refactor-agent-benchmark:py312",
        timeout_seconds: float = 120.0,
        memory: str = "512m",
        cpus: float = 1.0,
        repository_cache: BenchmarkRepositoryCache | None = None,
    ) -> None:
        if sandbox_backend != "docker":
            raise ValueError("External benchmarks require the Docker sandbox backend.")
        self.run_root = run_root.resolve()
        self.cache_root = cache_root.resolve()
        self.database_path = database_path or self.run_root / "benchmark.sqlite"
        self.docker_image = docker_image
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.repository_cache = repository_cache or BenchmarkRepositoryCache(self.cache_root)

    def run_case(self, case: BenchmarkCase, provider: str = "mock") -> BenchmarkCaseResult:
        case_root = self.run_root / "external" / case.name
        if case_root.exists():
            shutil.rmtree(case_root)
        checkout = case_root / "repository"
        started = perf_counter()
        try:
            self.repository_cache.prepare(case, checkout)
            _run_checked(["git", "apply", str(case.seed_patch)], checkout, self.timeout_seconds)
            target = checkout / Path(*case.target.split("/"))
            tests = checkout / Path(*case.tests.split("/"))
            if not target.is_file() or not tests.exists():
                raise RuntimeError("benchmark target or tests path is missing after checkout")
            _run_checked(
                build_benchmark_setup_command(
                    checkout,
                    self.docker_image,
                    self.memory,
                    self.cpus,
                ),
                checkout,
                self.timeout_seconds,
            )
            baseline = analyze_file(target)
            client = self._client(case, provider)
            result = RefactorOrchestrator(
                llm_client=client,
                run_root=case_root / "runs",
                store=SQLiteRunStore(self.database_path),
                pytest_timeout_seconds=self.timeout_seconds,
                sandbox_backend="docker",
                sandbox_docker_image=self.docker_image,
                sandbox_memory=self.memory,
                sandbox_cpus=self.cpus,
            ).run(
                RefactorRequest(
                    target_file=target,
                    issue_text=case.issue,
                    tests_path=tests,
                    repo_name=case.repository,
                    max_retry=3,
                    allowed_import_roots=set(case.allowed_import_roots),
                )
            )
            usage = _sum_usage(result.llm_usages)
            payload = {
                "case_name": case.name,
                "repository": case.repository,
                "commit": case.commit,
                "provider": provider,
                "model": usage[4],
                "status": result.record.status,
                "expected_status": case.expected_status,
                "failure_category": _classify_failure(result).value if result.record.status != "SUCCESS" else None,
                "attempts": result.attempts,
                "loc_before": baseline.loc,
                "loc_after": result.record.post_loc,
                "cc_before": baseline.cyclomatic_complexity,
                "cc_after": result.record.post_cc,
                "mutation_kill_rate": result.mutation_result.kill_rate if result.mutation_result else None,
                "adversarial_passed": result.adversarial_result.passed if result.adversarial_result else None,
                "prompt_tokens": usage[0],
                "completion_tokens": usage[1],
                "total_tokens": usage[2],
                "cost_usd": usage[3],
                "error": result.record.error,
            }
        except LLMError as exc:
            payload = {
                "case_name": case.name,
                "repository": case.repository,
                "commit": case.commit,
                "provider": provider,
                "model": "deepseek-chat",
                "status": "FAILED",
                "expected_status": case.expected_status,
                "failure_category": BenchmarkFailureCategory.PROVIDER.value,
                "error": str(exc)[:2048],
            }
        except subprocess.TimeoutExpired as exc:
            payload = {
                "case_name": case.name,
                "repository": case.repository,
                "commit": case.commit,
                "provider": provider,
                "model": "deterministic-gold" if provider == "mock" else "deepseek-chat",
                "status": "FAILED",
                "expected_status": case.expected_status,
                "failure_category": BenchmarkFailureCategory.TIMEOUT.value,
                "error": str(exc)[:2048],
            }
        except Exception as exc:
            payload = {
                "case_name": case.name,
                "repository": case.repository,
                "commit": case.commit,
                "provider": provider,
                "model": "deterministic-gold" if provider == "mock" else "deepseek-chat",
                "status": "FAILED",
                "expected_status": case.expected_status,
                "failure_category": BenchmarkFailureCategory.INFRASTRUCTURE.value,
                "error": str(exc)[:2048],
            }
        payload["runtime_seconds"] = perf_counter() - started
        payload["normalized_hash"] = normalized_result_hash(payload)
        return BenchmarkCaseResult.model_validate(payload)

    @staticmethod
    def _client(case: BenchmarkCase, provider: str) -> RefactorClient:
        if provider == "mock":
            return _GoldSnapshotClient(case.gold_snapshot.read_text(encoding="utf-8"))
        if provider == "deepseek":
            return DeepSeekClient()
        raise ValueError(f"unsupported benchmark provider: {provider}")


class _GoldSnapshotClient:
    def __init__(self, gold_source: str) -> None:
        self.gold_source = gold_source

    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        return LLMRefactorResult(
            thought="Apply the pinned gold AST region.",
            fixed_code=apply_gold_snapshot(current_code, self.gold_source),
            insult_review="Pinned deterministic benchmark repair.",
            usage=LLMUsage(
                provider="mock",
                model="deterministic-gold",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_usd=0,
            ),
        )


def apply_gold_snapshot(source: str, gold_source: str) -> str:
    source_nodes = _qualified_functions(ast.parse(source))
    gold_nodes = _qualified_functions(ast.parse(gold_source))
    matches = [(name, node) for name, node in gold_nodes.items() if name in source_nodes]
    if len(matches) != 1:
        raise ValueError("gold snapshot must identify exactly one matching AST region")
    name, gold_node = matches[0]
    source_node = source_nodes[name]
    source_lines = source.splitlines(keepends=True)
    gold_lines = gold_source.splitlines(keepends=True)
    replacement = gold_lines[gold_node.lineno - 1 : gold_node.end_lineno]
    if replacement and not replacement[-1].endswith(("\n", "\r")):
        replacement[-1] += "\n"
    source_lines[source_node.lineno - 1 : source_node.end_lineno] = replacement
    return "".join(source_lines)


def build_benchmark_setup_command(
    checkout: Path,
    docker_image: str,
    memory: str,
    cpus: float,
) -> list[str]:
    script = (
        "cp -a /workspace /tmp/repository && "
        "python -m pip install --no-deps -e /tmp/repository "
        "--no-build-isolation --target /tmp/install"
    )
    return [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--user", "65532:65532",
        "--tmpfs", "/tmp:rw,nosuid,size=512m",
        "--memory", memory,
        "--cpus", str(cpus),
        "-v", f"{checkout.resolve().as_posix()}:/workspace:ro",
        docker_image,
        "sh", "-lc", script,
    ]


def normalized_result_hash(payload: dict[str, object]) -> str:
    normalized = {
        key: value
        for key, value in payload.items()
        if key not in {"runtime_seconds", "generated_at", "normalized_hash", "error"}
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _qualified_functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            found[".".join([*self.classes, node.name])] = node

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            found[".".join([*self.classes, node.name])] = node

    Visitor().visit(tree)
    return found


def _run_checked(command: list[str], cwd: Path, timeout: float) -> None:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "benchmark command failed"
        raise RuntimeError(detail[:2048])


def _sum_usage(usages: list[LLMUsage]) -> tuple[int, int, int, float, str]:
    prompt = sum(item.prompt_tokens or 0 for item in usages)
    completion = sum(item.completion_tokens or 0 for item in usages)
    total = sum(item.total_tokens or 0 for item in usages)
    cost = sum(item.cost_usd or 0 for item in usages)
    model = usages[-1].model if usages else "unknown"
    return prompt, completion, total, cost, model


def _classify_failure(result) -> BenchmarkFailureCategory:
    error = (result.record.error or "").lower()
    if "deadline" in error or "timed out" in error:
        return BenchmarkFailureCategory.TIMEOUT
    if result.ast_validation is not None and not result.ast_validation.ok:
        return BenchmarkFailureCategory.AST_GUARD
    if result.last_sandbox_result is not None and not result.last_sandbox_result.passed:
        return BenchmarkFailureCategory.PYTEST
    if result.adversarial_result is not None and not result.adversarial_result.passed:
        return BenchmarkFailureCategory.ADVERSARY
    if result.mutation_result is not None and result.mutation_result.kill_rate < 1:
        return BenchmarkFailureCategory.MUTATION
    if "provider" in error or "deepseek" in error:
        return BenchmarkFailureCategory.PROVIDER
    return BenchmarkFailureCategory.TARGETING
