from __future__ import annotations

from pathlib import Path

from refactor_agent.ast_analyzer import analyze_ast
from refactor_agent.models import AdversarialTestResult
from refactor_agent.sandbox import run_pytest


def generate_adversarial_tests(source: str, module_name: str) -> str:
    analysis = analyze_ast(source)
    tests: list[str] = []
    imports: list[str] = []
    for function in analysis.functions:
        if len(function.args) == 1 and function.name.startswith("is_"):
            imports.append(function.name)
            tests.append(_boolean_probe(function.name))
        elif len(function.args) == 2 and function.name in {"add", "sum_two", "plus"}:
            imports.append(function.name)
            tests.append(_binary_add_probe(function.name))
    if not tests:
        return ""
    unique_imports = ", ".join(sorted(set(imports)))
    return f"from {module_name} import {unique_imports}\n\n\n" + "\n\n".join(tests) + "\n"


def run_adversarial_tests(
    candidate_source: str,
    workspace: Path,
    target_file: Path,
    timeout_seconds: float = 30.0,
) -> AdversarialTestResult:
    module_name = ".".join(target_file.relative_to(workspace).with_suffix("").parts)
    if module_name.endswith(".__init__"):
        module_name = module_name[:-9]
    generated_source = generate_adversarial_tests(candidate_source, module_name)
    if not generated_source:
        return AdversarialTestResult(generated=0, passed=True, returncode=0)

    tests_dir = workspace / ".adversary_tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / f"test_adversary_{target_file.stem}.py"
    test_file.write_text(generated_source, encoding="utf-8")
    result = run_pytest(workspace, test_file, timeout_seconds=timeout_seconds)
    return AdversarialTestResult(
        generated=_count_tests(generated_source),
        passed=result.passed,
        returncode=result.returncode,
        test_file=test_file,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _boolean_probe(function_name: str) -> str:
    return (
        f"def test_adversary_{function_name}_returns_bool_for_boundaries():\n"
        f"    for value in [-400, -1, 0, 1, 4, 100, 400, 1900, 2000, 2024]:\n"
        f"        assert {function_name}(value) in {{True, False}}\n"
    )


def _binary_add_probe(function_name: str) -> str:
    return (
        f"def test_adversary_{function_name}_commutative_and_identity():\n"
        f"    for left, right in [(-3, 7), (0, 0), (10, -10), (999, 1)]:\n"
        f"        assert {function_name}(left, right) == {function_name}(right, left)\n"
        f"        assert {function_name}(left, 0) == left\n"
    )


def _count_tests(source: str) -> int:
    return sum(1 for line in source.splitlines() if line.startswith("def test_"))
