from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from refactor_agent.models import RefactorRunResult

DEFAULT_DEMO_SUITE_CASES = ("add-maze", "adversarial-weekend", "business-day")


@dataclass(frozen=True)
class DemoSuiteRun:
    case_name: str
    title: str
    result: RefactorRunResult


def render_demo_suite_report(
    runs: list[DemoSuiteRun],
    run_root: Path,
    database: Path,
) -> str:
    successes = sum(1 for item in runs if item.result.record.status == "SUCCESS")
    total_loc_delta = sum(_delta_value(item.result.record.pre_loc, item.result.record.post_loc) for item in runs)
    total_cc_delta = sum(_delta_value(item.result.record.pre_cc, item.result.record.post_cc) for item in runs)
    total_retries = sum(item.result.record.self_heal_count for item in runs)
    lines = [
        "### 路演总战报 / Demo Suite Report",
        "",
        f"- 案例数: {len(runs)}",
        f"- 成功数: {successes}/{len(runs)}",
        f"- 总 LOC 变化: {total_loc_delta:+d}",
        f"- 总圈复杂度变化: {total_cc_delta:+d}",
        f"- 总自愈轮次: {total_retries}",
        f"- 数据库: `{database}`",
        f"- 运行目录: `{run_root}`",
        "- 竞技场命令: "
        f"`refactor-agent dashboard --run-root {run_root} --database {database}`",
        "",
        "#### 案例对比表",
        "",
        "| 案例 | 状态 | 自愈 | LOC | CC | Reward | 沙箱 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in runs:
        record = item.result.record
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(item.case_name),
                    _md(_status_cn(record.status)),
                    str(record.self_heal_count),
                    _md(_transition(record.pre_loc, record.post_loc)),
                    _md(_transition(record.pre_cc, record.post_cc)),
                    _md(_reward(item.result)),
                    _md(str(item.result.workspace_path)),
                ]
            )
            + " |"
        )

    lines.extend(["", "#### 现场串场词", ""])
    for item in runs:
        record = item.result.record
        lines.append(f"- {item.case_name}: {_punchline(item)}")
        if record.error:
            lines.append(f"  失败原因: {_compact(record.error, 220)}")
    return "\n".join(lines)


def _transition(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    return f"{before} -> {after} ({after - before:+d})"


def _delta_value(before: int | None, after: int | None) -> int:
    if before is None or after is None:
        return 0
    return after - before


def _reward(result: RefactorRunResult) -> str:
    for debate_round in reversed(result.debate_rounds):
        if debate_round.reward is not None:
            return f"{debate_round.reward.reward:.2f}"
    return "n/a"


def _punchline(item: DemoSuiteRun) -> str:
    record = item.result.record
    if record.status == "FAILED":
        return "这段代码还没通过裁决，适合现场展示失败保护和沙箱留痕。"
    loc_delta = _delta_value(record.pre_loc, record.post_loc)
    cc_delta = _delta_value(record.pre_cc, record.post_cc)
    if item.case_name == "adversarial-weekend" and record.self_heal_count:
        return "先让弱测试蒙混过关，再让对抗测试当场拆穿，节目效果刚好。"
    if loc_delta < 0 and cc_delta < 0:
        return f"代码从绕路现场被压成短句，LOC {loc_delta:+d}、CC {cc_delta:+d}，旧分支少了很多表演欲。"
    if record.self_heal_count:
        return f"自愈 {record.self_heal_count} 轮后通过，说明 Agent 不是一次性许愿机，失败会回炉。"
    return "通过闭环验证，适合用来说明 AST、沙箱、对抗测试和奖励函数如何合拍。"


def _status_cn(status: str) -> str:
    return {"SUCCESS": "成功", "FAILED": "失败"}.get(status, status)


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _compact(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
