from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from refactor_agent.models import RewardBreakdown, RunRecord, TrajectoryMemoryRecord

MAX_MEMORY_CONTEXT_CHARS = 1800


def target_memory_key(target_file: Path) -> str:
    """Use a stable, repo-portable key for repeated runs on the same logical file."""
    return target_file.name


def build_memory_context(records: list[TrajectoryMemoryRecord]) -> str | None:
    if not records:
        return None
    lines = [
        "历史轨迹记忆（State-Action-Reward Memory）：",
        "下面是同一目标文件过去的成功/失败经验。请优先避免重复踩坑，但不要为了迎合记忆牺牲测试正确性。",
    ]
    for index, record in enumerate(records, start=1):
        status = "成功" if record.status == "SUCCESS" else "失败"
        reward = f"，奖励分 {record.reward:.2f}" if record.reward is not None else ""
        signature = f"，错误签名：{record.error_signature}" if record.error_signature else ""
        lines.append(f"{index}. [{status}{reward}{signature}] {record.lesson}")
    context = "\n".join(lines)
    if len(context) <= MAX_MEMORY_CONTEXT_CHARS:
        return context
    return context[: MAX_MEMORY_CONTEXT_CHARS - 20].rstrip() + "\n...（历史记忆已截断）"


def success_memory(
    record: RunRecord,
    target_path: str,
    review: str | None,
    reward: RewardBreakdown | None,
) -> TrajectoryMemoryRecord:
    delta_loc = _delta_text(record.pre_loc, record.post_loc)
    delta_cc = _delta_text(record.pre_cc, record.post_cc)
    review_text = f" 毒舌审查摘要：{review}" if review else ""
    lesson = (
        f"这次候选通过了 pytest、AST 守卫、对抗测试和变异测试。"
        f"LOC 变化 {delta_loc}，圈复杂度变化 {delta_cc}，自愈 {record.self_heal_count} 次。"
        f"后续遇到类似结构时，可以复用这种更小但仍通过测试的表达方式。{review_text}"
    )
    return TrajectoryMemoryRecord(
        memory_id=_memory_id(),
        run_id=record.run_id,
        repo_name=record.repo_name,
        target_path=target_path,
        status="SUCCESS",
        lesson=lesson,
        reward=reward.reward if reward else None,
    )


def failure_memory(record: RunRecord, target_path: str) -> TrajectoryMemoryRecord:
    signature = error_signature(record.error)
    lesson = (
        "这次重构没有通过验证。下一轮不要只做表面压缩，必须优先修复错误签名对应的行为。"
        f"失败原因摘要：{_compact_error(record.error)}"
    )
    return TrajectoryMemoryRecord(
        memory_id=_memory_id(),
        run_id=record.run_id,
        repo_name=record.repo_name,
        target_path=target_path,
        status="FAILED",
        lesson=lesson,
        error_signature=signature,
    )


def error_signature(error: str | None) -> str | None:
    if not error:
        return None
    patterns = [
        r"AssertionError: .+",
        r"E\s+AssertionError: .+",
        r"E\s+[A-Za-z_][A-Za-z0-9_.]*Error: .+",
        r"[A-Za-z_][A-Za-z0-9_.]*Error: .+",
        r"failed with return code \d+",
        r"pytest 失败，返回码 \d+",
    ]
    for pattern in patterns:
        match = re.search(pattern, error)
        if match:
            return _one_line(match.group(0), limit=180)
    return _one_line(error, limit=180)


def _compact_error(error: str | None) -> str:
    if not error:
        return "无详细错误。"
    return _one_line(error[-800:], limit=300)


def _one_line(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact[: limit - 1] + "…" if len(compact) > limit else compact


def _delta_text(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    return f"{before}->{after}（{after - before:+d}）"


def _memory_id() -> str:
    return uuid4().hex
