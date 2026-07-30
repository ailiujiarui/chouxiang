from pathlib import Path

import pytest

from refactor_agent.mutation import generate_mutants, run_mutation_tests
from refactor_agent.sandbox import prepare_workspace


def test_generate_mutants_from_boolean_expression():
    source = "def is_even(value):\n    return value % 2 == 0\n"
    mutants = generate_mutants(source)
    assert mutants
    assert any("comparison" in mutant.description for mutant in mutants)


def test_generate_mutants_can_scope_to_changed_function():
    source = (
        "def unrelated(value):\n"
        "    return value == 1\n\n"
        "def target(value):\n"
        "    return value > 0\n"
    )

    mutants = generate_mutants(source, target_regions=["target"])

    assert mutants
    assert all("line 2" not in mutant.description for mutant in mutants)
    assert all("return value == 1" in mutant.source for mutant in mutants)


def test_generate_mutants_with_empty_scope_does_not_mutate_other_regions():
    source = "def unrelated(value):\n    return value == 1\n"

    assert generate_mutants(source, target_regions=[]) == []


def test_generate_mutants_rejects_missing_changed_region():
    source = "LIMIT = 2\n"

    with pytest.raises(ValueError, match="do not exist"):
        generate_mutants(source, target_regions=["module:1:If"])


def test_run_mutation_tests_reports_killed_mutants(tmp_path: Path):
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    source = "def is_even(value):\n    return value % 2 == 0\n"
    (project / "numbers.py").write_text(source, encoding="utf-8")
    (tests / "test_numbers.py").write_text(
        "from numbers import is_even\n\n\n"
        "def test_is_even():\n"
        "    assert is_even(2) is True\n"
        "    assert is_even(3) is False\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    _, target, tests_path = prepare_workspace(project / "numbers.py", tests, workspace)
    result = run_mutation_tests(source, target, workspace, tests_path, timeout_seconds=10)
    assert result.total >= 1
    assert result.killed >= 1
