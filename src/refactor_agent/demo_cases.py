from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DemoCase:
    name: str
    title: str
    target_filename: str
    issue_text: str
    source_code: str
    test_filename: str
    test_code: str


DEMO_CASES: dict[str, DemoCase] = {
    "leap-year": DemoCase(
        name="leap-year",
        title="Century leap-year bug",
        target_filename="leap_year.py",
        issue_text="1900 should not be a leap year, but 2000 and 2024 should be.",
        source_code=(
            "def is_leap_year(year):\n"
            "    if year % 4 == 0:\n"
            "        if year % 100 == 0:\n"
            "            return True\n"
            "        else:\n"
            "            return True\n"
            "    else:\n"
            "        return False\n"
        ),
        test_filename="test_leap_year.py",
        test_code=(
            "from leap_year import is_leap_year\n\n\n"
            "def test_leap_year_rules():\n"
            "    assert is_leap_year(2000) is True\n"
            "    assert is_leap_year(2024) is True\n"
            "    assert is_leap_year(1900) is False\n"
            "    assert is_leap_year(2023) is False\n"
        ),
    ),
    "add-maze": DemoCase(
        name="add-maze",
        title="Thirty-line integer addition maze",
        target_filename="math_maze.py",
        issue_text="The add(left, right) helper is bloated and still mishandles mixed signed values.",
        source_code=(
            "def add(left, right):\n"
            "    total = 0\n"
            "    if left > 0:\n"
            "        for _ in range(left):\n"
            "            total = total + 1\n"
            "    elif left < 0:\n"
            "        for _ in range(-left):\n"
            "            total = total - 1\n"
            "    else:\n"
            "        total = total + 0\n"
            "    if right > 0:\n"
            "        for _ in range(right):\n"
            "            total = total + 1\n"
            "    elif right < 0:\n"
            "        for _ in range(-right):\n"
            "            total = total - 1\n"
            "    else:\n"
            "        total = total + 0\n"
            "    if left == 0 and right == 0:\n"
            "        return 0\n"
            "    if total == left + right:\n"
            "        return total\n"
            "    return left + right\n"
        ),
        test_filename="test_math_maze.py",
        test_code=(
            "from math_maze import add\n\n\n"
            "def test_add_handles_signed_values():\n"
            "    assert add(2, 3) == 5\n"
            "    assert add(-2, 3) == 1\n"
            "    assert add(2, -3) == -1\n"
            "    assert add(-2, -3) == -5\n"
            "    assert add(0, 0) == 0\n"
        ),
    ),
    "business-day": DemoCase(
        name="business-day",
        title="Weekend predicate branch tangle",
        target_filename="calendar_rules.py",
        issue_text="is_business_day(day) should return True only for 1..5 and False for weekend values.",
        source_code=(
            "def is_business_day(day):\n"
            "    if day == 0:\n"
            "        return False\n"
            "    if day == 1:\n"
            "        return True\n"
            "    if day == 2:\n"
            "        return True\n"
            "    if day == 3:\n"
            "        return True\n"
            "    if day == 4:\n"
            "        return True\n"
            "    if day == 5:\n"
            "        return True\n"
            "    if day == 6:\n"
            "        return True\n"
            "    return False\n"
        ),
        test_filename="test_calendar_rules.py",
        test_code=(
            "from calendar_rules import is_business_day\n\n\n"
            "def test_business_days():\n"
            "    for day in [1, 2, 3, 4, 5]:\n"
            "        assert is_business_day(day) is True\n"
            "    for day in [0, 6, -1, 7]:\n"
            "        assert is_business_day(day) is False\n"
        ),
    ),
    "adversarial-weekend": DemoCase(
        name="adversarial-weekend",
        title="Weak tests that miss weekends until Adversary attacks",
        target_filename="calendar_rules.py",
        issue_text="is_business_day(day) should return True only for 1..5 and False for weekends and invalid values.",
        source_code=(
            "def is_business_day(day):\n"
            "    if day == 1:\n"
            "        return True\n"
            "    if day == 2:\n"
            "        return True\n"
            "    if day == 3:\n"
            "        return True\n"
            "    if day == 4:\n"
            "        return True\n"
            "    if day == 5:\n"
            "        return True\n"
            "    return False\n"
        ),
        test_filename="test_calendar_rules.py",
        test_code=(
            "from calendar_rules import is_business_day\n\n\n"
            "def test_business_days_weak_baseline():\n"
            "    assert is_business_day(1) is True\n"
            "    assert is_business_day(2) is True\n"
            "    assert is_business_day(0) is False\n"
        ),
    ),
}

DEMO_CASE_NAMES = tuple(DEMO_CASES)


def get_demo_case(name: str) -> DemoCase:
    try:
        return DEMO_CASES[name]
    except KeyError as exc:
        available = ", ".join(DEMO_CASE_NAMES)
        raise ValueError(f"Unknown demo case: {name}. Available cases: {available}") from exc


def materialize_demo_case(name: str, run_root: Path) -> tuple[Path, Path, Path]:
    case = get_demo_case(name)
    project = run_root / "_demo_cases" / case.name
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    target = project / case.target_filename
    issue = project / "issue.md"
    tests = tests_dir / case.test_filename

    target.write_text(case.source_code, encoding="utf-8")
    issue.write_text(case.issue_text + "\n", encoding="utf-8")
    tests.write_text(case.test_code, encoding="utf-8")
    return target, issue, tests_dir
