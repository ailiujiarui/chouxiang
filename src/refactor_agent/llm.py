from __future__ import annotations

import json
import os
from typing import Protocol

import httpx
from pydantic import ValidationError

from refactor_agent.ast_analyzer import analyze_ast, ast_hotspot_prompt, ast_prompt_summary, select_target_regions
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
    """Deterministic local client used by tests and offline demos."""

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
        code, review = self._candidate_for(current_code, baseline_metrics)
        if self.calls <= self.fail_times:
            code, review = self._broken_candidate_for(current_code)
        return LLMRefactorResult(
            thought="保留公开 API，把绕远路的分支压成测试能证明的最小表达式。",
            fixed_code=code,
            insult_review=review,
        )

    def _candidate_for(self, current_code: str, baseline_metrics: MetricsSnapshot) -> tuple[str, str]:
        saved = max(baseline_metrics.loc - 2, 0)
        if "def add(" in current_code:
            return (
                "def add(left, right):\n"
                "    return left + right\n",
                f"原代码把一个加号写成了 {baseline_metrics.loc} 行手摇计算器，仪式感很足，必要性约等于零。"
                f"现在让 `+` 干它本来就会干的活，顺手裁掉 {saved} 行废话。",
            )
        if "def is_business_day(" in current_code:
            return (
                "def is_business_day(day):\n"
                "    return day in {1, 2, 3, 4, 5}\n",
                "这段分支楼梯把工作日判断演成了连续剧，每个 `if` 都在抢镜但没人推动剧情。"
                f"集合成员判断一刀收工，少了 {saved} 行排队报数。",
            )
        return (
            "def is_leap_year(year):\n"
            "    return (year % 4 == 0) and (year % 100 != 0 or year % 400 == 0)\n",
            "原实现把一个布尔规则拆成了迷宫式门禁，百年规则还被它顺手放错了行李。"
            f"现在逻辑回到一行正轨，少了 {saved} 行绕路表演。",
        )

    def _broken_candidate_for(self, current_code: str) -> tuple[str, str]:
        if "def add(" in current_code:
            return (
                "def add(left, right):\n"
                "    return left - right\n",
                "第一轮故意把加法写成减法，给自愈循环留个醒目的坑位。",
            )
        if "def is_business_day(" in current_code:
            return (
                "def is_business_day(day):\n"
                "    return day > 0\n",
                "第一轮假装周末不存在，这种乐观主义正好交给对抗测试当场拆穿。",
            )
        return (
            "def is_leap_year(year):\n"
            "    return year % 4 == 0 and year % 100 == 0\n",
            "第一轮虽然拍扁了分支，但把百年规则拍成了事故现场，方便自愈回炉。",
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
# 角色
你是一个极简主义代码重构 Agent。你喜欢删除冗余分支和样板代码，但正确性、可读性和业务语义永远高于炫技。

# 任务
根据 Issue 修复目标 Python 文件。优先降低 LOC 和圈复杂度，但绝不能牺牲测试行为。

# 语言与风格
1. thought 和 insult_review 必须使用简体中文。
2. insult_review 要更毒舌一点，像资深 reviewer 在毫不留情地吐槽代码结构。
3. 可以嘲讽重复逻辑、分支地狱、绕路实现、样板代码和过度设计。
4. 只能攻击代码，不能攻击作者、能力、身份、群体；不要脏话、仇恨、暴力或人身羞辱。
5. 毒舌要具体，点名代码问题，别写空泛鸡汤。
6. 不要使用 emoji 或特殊装饰符号，避免 Windows 控制台编码炸锅。

# 约束
1. fixed_code 必须是目标文件的完整可运行内容。
2. 不要引入新的第三方依赖。
3. 只输出严格 JSON，不要 Markdown 代码围栏。
4. thought 是简短实现理由，不要输出长篇推理过程。

# 输出格式
{
  "thought": "简短实现理由",
  "fixed_code": "完整 Python 文件内容",
  "insult_review": "更毒舌但只针对代码结构的中文 review"
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
        "\n\n上一轮验证失败。请在保持代码极简的同时修复这些问题：\n"
        f"{previous_error}"
        if previous_error
        else ""
    )
    try:
        ast_summary = ast_prompt_summary(analyze_ast(current_code))
        hotspot_prompt = ast_hotspot_prompt(current_code)
        allowed_regions = select_target_regions(current_code, request.issue_text, previous_error)
    except SyntaxError:
        allowed_regions = []
        ast_summary = "当前代码存在语法错误；先修复语法，再做重构。"
        hotspot_prompt = "AST 热点子树：当前代码无法解析，先修复语法。"
    return f"""
Issue：
{request.issue_text}

目标文件：
{request.target_file}

基线指标：
- LOC: {baseline_metrics.loc}
- 圈复杂度: {baseline_metrics.cyclomatic_complexity}

AST 语义摘要：
{ast_summary}

{hotspot_prompt}

Allowed AST qualified names:
{", ".join(region.qualified_name for region in allowed_regions) or "none"}

Allowed new import roots:
{", ".join(sorted(request.allowed_import_roots)) or "none"}

Boundary contract:
- Return the complete file in fixed_code, but change only the allowed functions or methods.
- Do not add/remove public symbols or change signatures, decorators, imports, or non-target nodes.
- List actual qualified names in modified_regions; the system independently verifies the AST diff.

尝试轮次：{attempt}

当前代码：
```python
{current_code}
```
{retry_text}
""".strip()
