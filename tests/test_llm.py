import pytest

from refactor_agent.llm import LLMError, parse_llm_result


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
