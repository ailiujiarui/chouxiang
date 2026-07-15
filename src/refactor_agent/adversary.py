from __future__ import annotations

from pathlib import Path

from refactor_agent.ast_analyzer import analyze_ast
from refactor_agent.models import AdversarialCritique, AdversarialTestResult
from refactor_agent.sandbox import run_pytest_with_backend


def generate_adversarial_tests(source: str, module_name: str, issue_text: str = "") -> str:
    analysis = analyze_ast(source)
    tests: list[str] = []
    imports: list[str] = []
    for function in analysis.functions:
        if len(function.args) == 1 and function.name == "is_leap_year":
            imports.append(function.name)
            tests.append(_leap_year_probe(function.name))
        elif len(function.args) == 1 and function.name == "is_business_day":
            imports.append(function.name)
            tests.append(_business_day_probe(function.name))
        elif len(function.args) == 1 and function.name.startswith("is_"):
            imports.append(function.name)
            tests.append(_boolean_probe(function.name))
        elif len(function.args) == 2 and function.name in {"add", "sum_two", "plus"}:
            imports.append(function.name)
            tests.append(_binary_add_probe(function.name))
    if not tests:
        return ""
    unique_imports = ", ".join(sorted(set(imports)))
    return f"from {module_name} import {unique_imports}\n\n\n" + "\n\n".join(tests) + "\n"


def critique_candidate(source: str, issue_text: str) -> AdversarialCritique:
    analysis = analyze_ast(source)
    lowered_issue = issue_text.lower()
    attack_plan: list[str] = []
    risk_level = "LOW"
    counterexample_hint: str | None = None

    for function in analysis.functions:
        if function.name == "is_leap_year":
            attack_plan.append("猛攻世纪年：1900/2100 必须为 False，2000/2400 必须为 True。")
            counterexample_hint = "天真的 `year % 4 == 0` 通常会在 1900 或 2100 当场露馅。"
            risk_level = "HIGH"
        elif function.name == "is_business_day":
            attack_plan.append("猛攻 weekend/周末和越界值：0、6、7、-1。")
            counterexample_hint = "`day > 0` 能糊弄弱测试，但会把周末也请进工作日。"
            risk_level = "HIGH"
        elif function.name in {"add", "sum_two", "plus"}:
            attack_plan.append("猛攻交换律、加法单位元和正负数混合输入。")
            counterexample_hint = "`left - right` 在交换操作数前看着像那么回事，交换后就原形毕露。"
            risk_level = "MEDIUM"
        elif function.name.startswith("is_"):
            attack_plan.append(f"用边界值和非快乐路径输入围攻 `{function.name}`。")
            risk_level = "MEDIUM"

    if "weekend" in lowered_issue and not any("weekend" in item for item in attack_plan):
        attack_plan.append("Issue 提到 weekend，必须补显式周末反例，别让弱测试继续装睡。")
        risk_level = "HIGH"
    if not attack_plan:
        attack_plan.append("没有命中已知攻击启发式，退回变异测试继续拆台。")

    return AdversarialCritique(
        risk_level=risk_level,  # type: ignore[arg-type]
        attack_plan=attack_plan,
        counterexample_hint=counterexample_hint,
        rationale="红队审查专找弱基线测试容易漏掉的输入。",
    )


def run_adversarial_tests(
    candidate_source: str,
    workspace: Path,
    target_file: Path,
    issue_text: str = "",
    timeout_seconds: float = 30.0,
    backend: str = "subprocess",
    docker_image: str = "refactor-agent-sandbox:py312",
    memory: str = "256m",
    cpus: float = 1.0,
) -> AdversarialTestResult:
    module_name = ".".join(target_file.relative_to(workspace).with_suffix("").parts)
    if module_name.endswith(".__init__"):
        module_name = module_name[:-9]
    generated_source = generate_adversarial_tests(candidate_source, module_name, issue_text)
    if not generated_source:
        return AdversarialTestResult(generated=0, passed=True, returncode=0)

    tests_dir = workspace / ".adversary_tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / f"test_adversary_{target_file.stem}.py"
    test_file.write_text(generated_source, encoding="utf-8")
    result = run_pytest_with_backend(
        workspace=workspace,
        tests_path=test_file,
        timeout_seconds=timeout_seconds,
        backend=backend,
        docker_image=docker_image,
        memory=memory,
        cpus=cpus,
    )
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


def _leap_year_probe(function_name: str) -> str:
    return (
        f"def test_adversary_{function_name}_century_boundaries():\n"
        f"    assert {function_name}(1900) is False\n"
        f"    assert {function_name}(2100) is False\n"
        f"    assert {function_name}(2000) is True\n"
        f"    assert {function_name}(2400) is True\n"
        f"    assert {function_name}(2024) is True\n"
        f"    assert {function_name}(2023) is False\n"
    )


def _business_day_probe(function_name: str) -> str:
    return (
        f"def test_adversary_{function_name}_weekend_boundaries():\n"
        f"    for day in [1, 2, 3, 4, 5]:\n"
        f"        assert {function_name}(day) is True\n"
        f"    for day in [0, 6, 7, -1, 99]:\n"
        f"        assert {function_name}(day) is False\n"
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
