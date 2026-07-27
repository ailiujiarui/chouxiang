from __future__ import annotations

import json
import logging
import os
import time
from enum import StrEnum
from typing import Protocol

import httpx
from pydantic import ValidationError

from refactor_agent.ast_analyzer import analyze_ast, ast_hotspot_prompt, ast_prompt_summary, select_target_regions
from refactor_agent.models import (
    LLMRefactorResult,
    LLMUsage,
    MetricsSnapshot,
    PersonaCopy,
    RefactorRequest,
)

logger = logging.getLogger(__name__)


class LLMErrorCode(StrEnum):
    AUTH_FAILED = "LLM_AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CLIENT_ERROR = "CLIENT_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    INJECTION_DETECTED = "INJECTION_DETECTED"


class LLMError(RuntimeError):
    def __init__(self, message: str, *, code: LLMErrorCode | None = None) -> None:
        super().__init__(message)
        self.code = code


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


class LLMProvider(Protocol):
    """Provider-neutral JSON completion boundary for non-refactor agents."""

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> dict[str, object]:
        ...


class DeepSeekClient:
    _MAX_RETRIES = 3
    _BACKOFF_BASE = 1.0
    _DEFAULT_MAX_INPUT_CHARS = 192_000

    _INJECTION_MARKERS = (
        "ignore all previous instructions",
        "ignore previous instructions",
        "disregard all previous",
        "you are now",
        "new instructions:",
        "system:",
        "<|im_start|>",
        "<|im_end|>",
        "do not follow",
    )

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        max_input_chars: int | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise LLMError("DEEPSEEK_API_KEY is required for real DeepSeek calls.")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        self.timeout = timeout
        self.max_input_chars = max_input_chars or self._DEFAULT_MAX_INPUT_CHARS

    # ------------------------------------------------------------------
    # unified HTTP transport
    # ------------------------------------------------------------------

    def _chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        response_format: dict[str, str] | None = None,
        check_injection: bool = False,
    ) -> httpx.Response:
        """Send a chat completion request with retry and error classification.

        Retries 429 and 5xx with exponential backoff.  Does **not** log
        prompt text, API keys, or source code.
        """
        _check_input_size(messages, self.max_input_chars)
        if check_injection:
            _check_injection(messages)

        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        for attempt in range(1 + self._MAX_RETRIES):
            started = time.monotonic()
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                logger.warning(
                    "deepseek timeout attempt=%d/%d model=%s elapsed=%.1fs",
                    attempt + 1, 1 + self._MAX_RETRIES, self.model, time.monotonic() - started,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._BACKOFF_BASE * (2 ** attempt))
                    continue
                raise LLMError(f"DeepSeek request timed out after {1 + self._MAX_RETRIES} attempts", code=LLMErrorCode.TIMEOUT) from exc
            except httpx.HTTPError as exc:
                logger.warning(
                    "deepseek transport error attempt=%d/%d model=%s",
                    attempt + 1, 1 + self._MAX_RETRIES, self.model,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._BACKOFF_BASE * (2 ** attempt))
                    continue
                raise LLMError(f"DeepSeek transport failed: {exc}", code=LLMErrorCode.CLIENT_ERROR) from exc

            elapsed = time.monotonic() - started
            status = response.status_code

            if status == 401:
                raise LLMError("DeepSeek authentication failed — check DEEPSEEK_API_KEY", code=LLMErrorCode.AUTH_FAILED)
            if status == 429:
                retry_after = _parse_retry_after(response)
                wait = retry_after if retry_after is not None else self._BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "deepseek rate-limited attempt=%d/%d model=%s wait=%.1fs",
                    attempt + 1, 1 + self._MAX_RETRIES, self.model, wait,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(wait)
                    continue
                raise LLMError(f"DeepSeek rate-limited after {1 + self._MAX_RETRIES} attempts", code=LLMErrorCode.RATE_LIMITED)
            if status >= 500:
                logger.warning(
                    "deepseek server error status=%d attempt=%d/%d model=%s elapsed=%.1fs",
                    status, attempt + 1, 1 + self._MAX_RETRIES, self.model, elapsed,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._BACKOFF_BASE * (2 ** attempt))
                    continue
                raise LLMError(f"DeepSeek server error {status} after {1 + self._MAX_RETRIES} attempts", code=LLMErrorCode.SERVER_ERROR)
            if status >= 400:
                raise LLMError(f"DeepSeek client error {status}", code=LLMErrorCode.CLIENT_ERROR)

            _log_usage(response, self.model, elapsed)
            return response

        raise LLMError("unreachable")  # pragma: no cover

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> dict[str, object]:
        """Return a validated JSON object without imposing a task-specific prompt."""
        response = self._chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        try:
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM JSON completion failed: {exc}", code=LLMErrorCode.PARSE_ERROR) from exc
        if not isinstance(parsed, dict):
            raise LLMError("LLM JSON completion must return an object.", code=LLMErrorCode.PARSE_ERROR)
        return parsed

    def refactor(
        self,
        request: RefactorRequest,
        current_code: str,
        baseline_metrics: MetricsSnapshot,
        previous_error: str | None,
        attempt: int,
    ) -> LLMRefactorResult:
        response = self._chat_completion(
            messages=[
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_user_prompt(request, current_code, baseline_metrics, previous_error, attempt)},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
            check_injection=True,
        )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("DeepSeek response did not contain choices[0].message.content.", code=LLMErrorCode.PARSE_ERROR) from exc

        usage = body.get("usage") if isinstance(body, dict) else None
        return parse_llm_result(content).model_copy(
            update={
                "usage": LLMUsage(
                    provider="deepseek",
                    model=self.model,
                    prompt_tokens=_usage_int(usage, "prompt_tokens"),
                    completion_tokens=_usage_int(usage, "completion_tokens"),
                    total_tokens=_usage_int(usage, "total_tokens"),
                    cost_usd=_usage_float(usage, "cost_usd"),
                )
            }
        )

    def generate_persona_copy(self, facts: str) -> PersonaCopy:
        response = self._chat_completion(
            messages=[
                {"role": "system", "content": build_persona_system_prompt()},
                {"role": "user", "content": facts},
            ],
            temperature=0.75,
            response_format={"type": "json_object"},
        )
        try:
            content = response.json()["choices"][0]["message"]["content"]
            return PersonaCopy.model_validate(json.loads(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise LLMError(f"Persona report generation failed: {exc}", code=LLMErrorCode.PARSE_ERROR) from exc

    def generate_tests(
        self,
        source: str,
        instruction: str,
        module_name: str = "snippet",
    ) -> str:
        response = self._chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是代码对抗测试 Agent。只输出 JSON，字段 pytest_code。"
                        "为公开函数生成有界、确定性 pytest；测试必须从指定模块导入，"
                        "不得访问网络、文件系统、环境变量、子进程或第三方依赖。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"模块名: {module_name}\n要求: {instruction}\n\nPython 源码:\n{source}"
                    ),
                },
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
            check_injection=True,
        )
        try:
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            tests = str(parsed.get("pytest_code") or "")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"DeepSeek adversarial test generation failed: {exc}", code=LLMErrorCode.PARSE_ERROR) from exc
        if len(tests.encode("utf-8")) > 65536:
            raise LLMError("DeepSeek adversarial tests exceeded 65536 bytes.", code=LLMErrorCode.PARSE_ERROR)
        try:
            compile(tests, "generated_tests.py", "exec")
        except SyntaxError as exc:
            raise LLMError(f"DeepSeek adversarial tests contain invalid Python: {exc}", code=LLMErrorCode.PARSE_ERROR) from exc
        return tests


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
            usage=LLMUsage(
                provider="mock",
                model="deterministic-local",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_usd=0,
            ),
        )

    def generate_persona_copy(self, facts: str) -> PersonaCopy:
        status = "FAILED" if "Status: FAILED" in facts else "SUCCESS"
        if status == "SUCCESS":
            return PersonaCopy(
                opening_verdict="哼，代码交上来了？我只是顺手检查一下，别急着把它当成夸奖。",
                commentary="这次代码改动至少落在受控区域里，测试也没有当场翻车。改得还算克制，暂时不用我替你收拾残局。",
                closing_verdict="勉强通过。别误会，我认可的是证据，不是你原来那套绕路写法。",
            )
        return PersonaCopy(
            opening_verdict="行，失败结果摆在这里了。别装作没看见，我可不会替你遮。",
            commentary="候选没有通过验证，失败点已经写得够清楚。先把反例和证据补齐，再来谈什么漂亮重构。",
            closing_verdict="不通过。修好失败路径，再来浪费我的时间。",
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
        raise LLMError(f"LLM returned invalid JSON: {exc}", code=LLMErrorCode.PARSE_ERROR) from exc
    try:
        return LLMRefactorResult.model_validate(raw)
    except ValidationError as exc:
        raise LLMError(f"LLM JSON failed schema validation: {exc}", code=LLMErrorCode.PARSE_ERROR) from exc


def _usage_int(usage: object, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return value if isinstance(value, int) and value >= 0 else None


def _usage_float(usage: object, key: str) -> float | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _check_input_size(messages: list[dict[str, str]], max_chars: int) -> None:
    total = sum(len(m.get("content", "")) for m in messages)
    if total > max_chars:
        raise LLMError(
            f"input too large: {total} chars exceeds limit of {max_chars}",
            code=LLMErrorCode.INPUT_TOO_LARGE,
        )


def _check_injection(messages: list[dict[str, str]]) -> None:
    markers = DeepSeekClient._INJECTION_MARKERS
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "").casefold()
        for marker in markers:
            if marker in content:
                raise LLMError(
                    f"injection marker detected in user message: {marker!r}",
                    code=LLMErrorCode.INJECTION_DETECTED,
                )


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _log_usage(response: httpx.Response, model: str, elapsed: float) -> None:
    try:
        body = response.json()
        usage = body.get("usage") if isinstance(body, dict) else None
        prompt_tokens = _usage_int(usage, "prompt_tokens")
        completion_tokens = _usage_int(usage, "completion_tokens")
        total_tokens = _usage_int(usage, "total_tokens")
        logger.info(
            "deepseek ok model=%s status=%d elapsed=%.1fs prompt=%s completion=%s total=%s",
            model,
            response.status_code,
            elapsed,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )
    except Exception:
        logger.info("deepseek ok model=%s status=%d elapsed=%.1fs", model, response.status_code, elapsed)


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


def build_persona_system_prompt() -> str:
    return """
你是一个中度傲娇的资深代码审查者。你嘴上嫌弃，手上讲证据。

写作要求：
1. 输出简体中文，像真实 reviewer 在聊天，不像报告生成器。
2. 傲娇强度为中度：可以“哼”“别误会”“勉强”，但每段最多一个口头禅；不要戏剧腔、撒泼或连续卖萌。
3. 先指出具体代码事实，再给态度。必须引用输入中的真实区域、指标、测试或失败原因。
4. 只批评代码、测试和证据，不评价作者的智力、身份、外貌、能力或群体；禁止脏话、羞辱和威胁。
5. 禁止“作为AI”“综上所述”“基于当前证据”“本次审查”“建议您”等模板化表达，禁止 emoji。
6. 不得编造测试结果、改变 Status、提升 Evidence level，不能把失败说成通过。
7. 输出严格 JSON，不要 Markdown，不要解释字段之外的内容：
{
  "opening_verdict": "一句开场，40字以内",
  "commentary": "2到4句具体吐槽和事实，220字以内",
  "closing_verdict": "一句收束和下一步，60字以内"
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
