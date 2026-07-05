from __future__ import annotations

import json
import os
from typing import Protocol

import httpx
from pydantic import ValidationError

from refactor_agent.models import LLMRefactorResult, MetricsSnapshot, RefactorRequest


class LLMError(RuntimeError):
    pass


class RefactorClient(Protocol):
    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        ...


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise LLMError("DEEPSEEK_API_KEY is required for real DeepSeek calls.")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        self.timeout = timeout

    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_system_prompt()},
                {
                    "role": "user",
                    "content": build_user_prompt(request, current_code, baseline_metrics, previous_error, attempt),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"DeepSeek request failed: {exc}") from exc

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("DeepSeek response did not contain choices[0].message.content.") from exc

        return parse_llm_result(content)


class MockRefactorClient:
    """Deterministic local client used by tests and the built-in demo."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            code = "def is_leap_year(year):\n    return year % 4 == 0 and year % 100 == 0\n"
            review = "先把分支拆了，但这版故意保留一个逻辑瑕疵用于自愈测试。"
        else:
            code = (
                "def is_leap_year(year):\n"
                "    return (year % 4 == 0) * (year % 100 != 0) + (year % 400 == 0) > 0\n"
            )
            saved = max(baseline_metrics.loc - 2, 0)
            review = (
                "原实现把一个布尔表达式写成了条件迷宫。"
                f"现在用一行布尔算术收束逻辑，少走了 {saved} 行弯路。"
            )
        return LLMRefactorResult(
            thought="Use the canonical leap-year predicate and preserve the public function name.",
            fixed_code=code,
            insult_review=review,
        )


def parse_llm_result(content: str) -> LLMRefactorResult:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
    try:
        return LLMRefactorResult.model_validate(raw)
    except ValidationError as exc:
        raise LLMError(f"LLM JSON failed schema validation: {exc}") from exc


def build_system_prompt() -> str:
    return """
# Role
你是一个极简主义代码重构 Agent。你喜欢删掉冗余分支，但必须尊重测试、可读性和业务语义。

# Task
根据 Issue 描述修复目标 Python 代码。优先降低 LOC 和圈复杂度，但不能牺牲正确性。

# Constraints
1. fixed_code 必须是完整可运行的目标文件内容。
2. 不要引入新的第三方库。
3. 输出必须是严格 JSON，不要包含 Markdown 代码围栏。
4. insult_review 可以讽刺代码结构，但只能批评代码，不做人身攻击。
5. thought 只写简短实现理由，不要输出冗长推理过程。

# Output Format
{
  "thought": "简短说明修复策略",
  "fixed_code": "完整 Python 文件内容",
  "insult_review": "针对原代码冗余结构的简短评论"
}
""".strip()


def build_user_prompt(
    request: RefactorRequest,
    current_code: str,
    baseline_metrics: MetricsSnapshot,
    previous_error: str | None,
    attempt: int,
) -> str:
    retry_text = (
        "\n\n上一轮 pytest 失败信息如下，请修复它并保持代码简洁：\n"
        f"{previous_error}"
        if previous_error
        else ""
    )
    return f"""
Issue:
{request.issue_text}

Target file:
{request.target_file}

Baseline metrics:
- LOC: {baseline_metrics.loc}
- Cyclomatic Complexity: {baseline_metrics.cyclomatic_complexity}

Attempt: {attempt}

Current code:
```python
{current_code}
```
{retry_text}
""".strip()
