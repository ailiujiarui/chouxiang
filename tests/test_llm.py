import pytest

from refactor_agent.llm import LLMError, build_user_prompt, parse_llm_result
from refactor_agent.models import MetricsSnapshot, RefactorRequest


def test_parse_llm_result_success():
    result = parse_llm_result(
        '{"thought":"short","fixed_code":"def f():\\n    return 1\\n","insult_review":"too many branches"}'
    )
    assert result.fixed_code.startswith("def f")


def test_parse_llm_result_rejects_missing_field():
    with pytest.raises(LLMError):
        parse_llm_result('{"thought":"short","fixed_code":"x = 1"}')


def test_parse_llm_result_rejects_invalid_json():
    with pytest.raises(LLMError):
        parse_llm_result("not json")


def test_build_user_prompt_includes_ast_hotspots(tmp_path):
    source = (
        "def messy(value):\n"
        "    if value > 10:\n"
        "        return 'big'\n"
        "    if value > 0:\n"
        "        return 'small'\n"
        "    if value == 0:\n"
        "        return 'zero'\n"
        "    return 'negative'\n"
    )
    request = RefactorRequest(
        target_file=tmp_path / "sample.py",
        issue_text="simplify messy branching",
        tests_path=tmp_path / "tests",
    )

    prompt = build_user_prompt(
        request=request,
        current_code=source,
        baseline_metrics=MetricsSnapshot(loc=8, cyclomatic_complexity=4),
        previous_error=None,
        attempt=1,
    )

    assert "AST 热点子树" in prompt
    assert "`messy`" in prompt
    assert "结构熵" in prompt
