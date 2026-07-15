from pathlib import Path

from refactor_agent.locator import locate_source_file


def test_locate_source_file_by_filename_tokens_and_ast_symbols(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "leap_year.py").write_text(
        "def is_leap_year(year):\n    return year % 4 == 0\n",
        encoding="utf-8",
    )
    (repo / "billing.py").write_text(
        "def calculate_invoice(total):\n    return total\n",
        encoding="utf-8",
    )
    located = locate_source_file(repo, "Leap year bug: is_leap_year returns true for 1900")
    assert located is not None
    assert located.path == "leap_year.py"
    assert located.score >= 20


def test_locate_source_file_returns_none_when_no_signal(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "billing.py").write_text("def calculate_invoice(total):\n    return total\n", encoding="utf-8")
    assert locate_source_file(repo, "The user interface color is wrong") is None
