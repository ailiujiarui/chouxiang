from pathlib import Path

import pytest

from refactor_agent.llm import DeepSeekClient, LLMError, MockRefactorClient, build_user_prompt, parse_llm_result
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


def test_mock_client_reports_zero_usage(tmp_path: Path):
    result = MockRefactorClient().refactor(
        request=RefactorRequest(
            target_file=tmp_path / "value.py",
            issue_text="fix leap year",
            tests_path=tmp_path / "tests",
        ),
        current_code="def is_leap_year(year):\n    return year % 4 == 0\n",
        baseline_metrics=MetricsSnapshot(loc=2, cyclomatic_complexity=1),
        previous_error=None,
        attempt=1,
    )

    assert result.usage is not None
    assert result.usage.provider == "mock"
    assert result.usage.total_tokens == 0
    assert result.usage.cost_usd == 0


def test_deepseek_client_parses_usage_metadata(monkeypatch, tmp_path: Path):
    response = _DeepSeekResponse(
        usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    )
    monkeypatch.setattr("refactor_agent.llm.httpx.post", lambda *args, **kwargs: response)

    result = _deepseek_result(tmp_path)

    assert result.usage is not None
    assert result.usage.provider == "deepseek"
    assert result.usage.model == "deepseek-chat"
    assert result.usage.prompt_tokens == 120
    assert result.usage.completion_tokens == 30
    assert result.usage.total_tokens == 150


def test_deepseek_client_allows_missing_usage(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "refactor_agent.llm.httpx.post",
        lambda *args, **kwargs: _DeepSeekResponse(),
    )

    result = _deepseek_result(tmp_path)

    assert result.usage is not None
    assert result.usage.total_tokens is None


def test_deepseek_client_generates_bounded_pytest(monkeypatch):
    class TestResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"pytest_code":"from snippet import value\\n\\n'
                                'def test_value():\\n    assert value() == 1\\n"}'
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("refactor_agent.llm.httpx.post", lambda *args, **kwargs: TestResponse())
    tests = DeepSeekClient(api_key="test-key").generate_tests(
        "def value():\n    return 1\n",
        "simplify value",
    )
    assert "from snippet import value" in tests
    assert "def test_value" in tests


class _DeepSeekResponse:
    def __init__(self, usage=None):
        self.usage = usage

    def raise_for_status(self):
        return None

    def json(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"thought":"short","fixed_code":"def f():\\n    return 1\\n",'
                            '"insult_review":"branches"}'
                        )
                    }
                }
            ]
        }
        if self.usage is not None:
            payload["usage"] = self.usage
        return payload


def _deepseek_result(tmp_path: Path):
    return DeepSeekClient(api_key="test-key", model="deepseek-chat").refactor(
        request=RefactorRequest(
            target_file=tmp_path / "value.py",
            issue_text="fix f",
            tests_path=tmp_path / "tests",
        ),
        current_code="def f():\n    return 0\n",
        baseline_metrics=MetricsSnapshot(loc=2, cyclomatic_complexity=1),
        previous_error=None,
        attempt=1,
    )


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
