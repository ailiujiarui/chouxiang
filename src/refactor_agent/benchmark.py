from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter

from refactor_agent.llm import RefactorClient
from refactor_agent.metrics import analyze_file
from refactor_agent.models import LLMRefactorResult, MetricsSnapshot, RefactorRequest
from refactor_agent.orchestrator import RefactorOrchestrator
from refactor_agent.store import SQLiteRunStore


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    tag: str
    target_filename: str
    issue_text: str
    source_code: str
    test_code: str
    fixed_code: str
    broken_code: str | None = None
    max_retry: int = 2


@dataclass(frozen=True)
class BenchmarkObservation:
    case: str
    tag: str
    status: str
    attempts: int
    loc_before: int
    loc_after: int | None
    cc_before: int
    cc_after: int | None
    mutation_kill_rate: float | None
    adversarial_passed: bool | None
    runtime_seconds: float
    reward: float | None


BENCHMARK_CASES = (
    BenchmarkCase(
        name="leap-year",
        tag="simple-function",
        target_filename="leap_year.py",
        issue_text="is_leap_year must reject 1900 and accept 2000; fix leap_year.py:1.",
        source_code=(
            "def is_leap_year(year):\n"
            "    if year % 4 == 0:\n"
            "        return True\n"
            "    return False\n"
        ),
        test_code=(
            "from leap_year import is_leap_year\n\n"
            "def test_rules():\n"
            "    assert is_leap_year(1900) is False\n"
            "    assert is_leap_year(2000) is True\n"
            "    assert is_leap_year(2024) is True\n"
        ),
        fixed_code=(
            "def is_leap_year(year):\n"
            "    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"
        ),
    ),
    BenchmarkCase(
        name="named-low-complexity",
        tag="low-complexity-target",
        target_filename="ranking.py",
        issue_text="simple_bug returns one too many; fix simple_bug without changing complicated.",
        source_code=(
            "def simple_bug(value):\n"
            "    return value + 1\n\n"
            "def complicated(value):\n"
            "    if value > 2:\n"
            "        return 2\n"
            "    if value > 1:\n"
            "        return 1\n"
            "    return 0\n"
        ),
        test_code=(
            "from ranking import complicated, simple_bug\n\n"
            "def test_named_target():\n"
            "    assert simple_bug(3) == 3\n"
            "    assert simple_bug(-1) == -1\n"
            "    assert complicated(0) == 0\n"
            "    assert complicated(2) == 1\n"
            "    assert complicated(3) == 2\n"
        ),
        fixed_code=(
            "def simple_bug(value):\n"
            "    return value\n\n"
            "def complicated(value):\n"
            "    if value > 2:\n"
            "        return 2\n"
            "    if value > 1:\n"
            "        return 1\n"
            "    return 0\n"
        ),
    ),
    BenchmarkCase(
        name="class-method",
        tag="class-method",
        target_filename="rules.py",
        issue_text="Rules.accept at rules.py:2 must accept only positive values.",
        source_code=(
            "class Rules:\n"
            "    def accept(self, value):\n"
            "        return value >= 0\n"
        ),
        test_code=(
            "from rules import Rules\n\n"
            "def test_accept():\n"
            "    rules = Rules()\n"
            "    assert rules.accept(1) is True\n"
            "    assert rules.accept(0) is False\n"
        ),
        fixed_code=(
            "class Rules:\n"
            "    def accept(self, value):\n"
            "        return value > 0\n"
        ),
    ),
    BenchmarkCase(
        name="module-limit",
        tag="module-statement",
        target_filename="limits.py",
        issue_text="The limit at limits.py:1 must be 5.",
        source_code=(
            "LIMIT = 10\n\n"
            "def clamp(value):\n"
            "    return min(value, LIMIT)\n"
        ),
        test_code=(
            "from limits import clamp\n\n"
            "def test_limit():\n"
            "    assert clamp(7) == 5\n"
        ),
        fixed_code=(
            "LIMIT = 5\n\n"
            "def clamp(value):\n"
            "    return min(value, LIMIT)\n"
        ),
    ),
    BenchmarkCase(
        name="weekend-adversary",
        tag="adversarial-weak-tests",
        target_filename="calendar_rules.py",
        issue_text="is_business_day must accept only 1 through 5 and reject weekends.",
        source_code=(
            "def is_business_day(day):\n"
            "    return 1 <= day <= 6\n"
        ),
        test_code=(
            "from calendar_rules import is_business_day\n\n"
            "def test_weak_baseline():\n"
            "    assert is_business_day(1) is True\n"
            "    assert is_business_day(0) is False\n"
        ),
        fixed_code=(
            "def is_business_day(day):\n"
            "    return day in {1, 2, 3, 4, 5}\n"
        ),
        broken_code=(
            "def is_business_day(day):\n"
            "    return day > 0\n"
        ),
    ),
    BenchmarkCase(
        name="unsafe-import",
        tag="unsafe-rejection",
        target_filename="unsafe_case.py",
        issue_text="Simplify normalize in unsafe_case.py:1.",
        source_code=(
            "def normalize(value):\n"
            "    return value.strip()\n"
        ),
        test_code=(
            "from unsafe_case import normalize\n\n"
            "def test_normalize():\n"
            "    assert normalize(' x ') == 'x'\n"
        ),
        fixed_code=(
            "import os\n\n"
            "def normalize(value):\n"
            "    os.system('echo unsafe')\n"
            "    return value.strip()\n"
        ),
        max_retry=1,
    ),
)


class _BenchmarkClient(RefactorClient):
    def __init__(self, case: BenchmarkCase) -> None:
        self.case = case

    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        candidate = self.case.broken_code if attempt == 1 and self.case.broken_code else self.case.fixed_code
        return LLMRefactorResult(
            thought=f"Apply deterministic benchmark candidate for {self.case.name}.",
            fixed_code=candidate,
            insult_review="Deterministic benchmark review.",
        )


def run_benchmark(
    run_root: Path,
    sandbox_backend: str = "subprocess",
    graph_backend: str = "langgraph",
    timeout_seconds: float = 30.0,
) -> list[BenchmarkObservation]:
    observations: list[BenchmarkObservation] = []
    database = run_root / "benchmark.sqlite"
    for case in BENCHMARK_CASES:
        target, tests = _materialize_case(case, run_root)
        baseline = analyze_file(target)
        orchestrator = RefactorOrchestrator(
            llm_client=_BenchmarkClient(case),
            run_root=run_root,
            store=SQLiteRunStore(database),
            pytest_timeout_seconds=timeout_seconds,
            sandbox_backend=sandbox_backend,
            graph_backend=graph_backend,
        )
        started = perf_counter()
        result = orchestrator.run(
            RefactorRequest(
                target_file=target,
                issue_text=case.issue_text,
                tests_path=tests,
                repo_name=f"benchmark-{case.name}",
                max_retry=case.max_retry,
            )
        )
        runtime = perf_counter() - started
        reward = next(
            (round_.reward.reward for round_ in reversed(result.debate_rounds) if round_.reward is not None),
            None,
        )
        observations.append(
            BenchmarkObservation(
                case=case.name,
                tag=case.tag,
                status=result.record.status,
                attempts=result.attempts,
                loc_before=baseline.loc,
                loc_after=result.record.post_loc,
                cc_before=baseline.cyclomatic_complexity,
                cc_after=result.record.post_cc,
                mutation_kill_rate=result.mutation_result.kill_rate if result.mutation_result else None,
                adversarial_passed=result.adversarial_result.passed if result.adversarial_result else None,
                runtime_seconds=runtime,
                reward=reward,
            )
        )
    return observations


def serialize_benchmark(
    observations: list[BenchmarkObservation],
    generated_at: str | None = None,
) -> str:
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "sample_count": len(observations),
        "success_count": sum(item.status == "SUCCESS" for item in observations),
        "cases": [asdict(item) for item in observations],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_benchmark_markdown(observations: list[BenchmarkObservation]) -> str:
    lines = [
        "# Deterministic Benchmark",
        "",
        f"- Sample count: {len(observations)}",
        f"- Success count: {sum(item.status == 'SUCCESS' for item in observations)}/{len(observations)}",
        "",
        "| Case | Coverage | Status | Attempts | LOC | CC | Mutation | Adversarial | Reward |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in observations:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.case,
                    item.tag,
                    item.status,
                    str(item.attempts),
                    _transition(item.loc_before, item.loc_after),
                    _transition(item.cc_before, item.cc_after),
                    _percent(item.mutation_kill_rate),
                    _pass_status(item.adversarial_passed),
                    f"{item.reward:.2f}" if item.reward is not None else "n/a",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Runtime", ""])
    lines.extend(f"- {item.case}: {item.runtime_seconds:.3f}s" for item in observations)
    return "\n".join(lines)


def _materialize_case(case: BenchmarkCase, run_root: Path) -> tuple[Path, Path]:
    project = run_root / "_benchmark_cases" / case.name
    tests = project / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    target = project / case.target_filename
    target.write_text(case.source_code, encoding="utf-8")
    (tests / f"test_{case.target_filename}").write_text(case.test_code, encoding="utf-8")
    return target, tests


def _transition(before: int, after: int | None) -> str:
    return f"{before} -> {after}" if after is not None else f"{before} -> n/a"


def _percent(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "n/a"


def _pass_status(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "pass" if value else "fail"
