from pathlib import Path

from refactor_agent.adversary import critique_candidate, generate_adversarial_tests, run_adversarial_tests


def test_generate_adversarial_tests_for_boolean_predicate():
    source = "def is_ready(value):\n    return value > 0\n"
    generated = generate_adversarial_tests(source, "sample")
    assert "from sample import is_ready" in generated
    assert "test_adversary_is_ready_returns_bool_for_boundaries" in generated


def test_generate_adversarial_tests_for_business_day_semantics():
    source = "def is_business_day(day):\n    return day > 0\n"
    generated = generate_adversarial_tests(source, "calendar_rules")
    assert "assert is_business_day(day) is False" in generated
    assert "6, 7, -1" in generated


def test_critique_candidate_names_counterexample():
    source = "def is_business_day(day):\n    return day > 0\n"
    critique = critique_candidate(source, "weekends should be false")
    assert critique.risk_level == "HIGH"
    assert "weekend" in " ".join(critique.attack_plan).lower()
    assert critique.counterexample_hint is not None


def test_run_adversarial_tests_passes_boolean_contract(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "flags.py"
    source = "def is_ready(value):\n    return value > 0\n"
    target.write_text(source, encoding="utf-8")
    result = run_adversarial_tests(source, workspace, target, timeout_seconds=10)
    assert result.generated == 1
    assert result.passed is True
    assert result.test_file is not None


def test_run_adversarial_tests_fails_bad_boolean_contract(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "flags.py"
    source = "def is_ready(value):\n    return 'yes'\n"
    target.write_text(source, encoding="utf-8")
    result = run_adversarial_tests(source, workspace, target, timeout_seconds=10)
    assert result.generated == 1
    assert result.passed is False


def test_run_adversarial_tests_fails_weak_business_day_candidate(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "calendar_rules.py"
    source = "def is_business_day(day):\n    return day > 0\n"
    target.write_text(source, encoding="utf-8")
    result = run_adversarial_tests(source, workspace, target, timeout_seconds=10)
    assert result.generated == 1
    assert result.passed is False
