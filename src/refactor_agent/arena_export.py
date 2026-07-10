from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from refactor_agent.dashboard import DashboardRun, build_agent_chat_messages, load_dashboard_runs


def write_arena_report(
    database_path: Path,
    run_root: Path,
    output_path: Path,
    limit: int = 20,
) -> Path:
    runs = load_dashboard_runs(database_path, run_root, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_arena_report(runs), encoding="utf-8")
    return output_path


def render_arena_report(
    runs: list[DashboardRun],
    generated_at: str | None = None,
) -> str:
    generated = generated_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    successes = sum(1 for item in runs if item.record.status == "SUCCESS")
    total_loc_delta = sum(item.loc_delta or 0 for item in runs)
    total_cc_delta = sum(item.cc_delta or 0 for item in runs)
    avg_retry = _average([item.record.self_heal_count for item in runs])
    avg_reward = _average_float([item.reward for item in runs if item.reward is not None])

    lines = [
        "# 重构 Agent 竞技场战报",
        "",
        f"- 生成时间: {generated}",
        f"- 运行数: {len(runs)}",
        f"- 成功率: {_percent(successes, len(runs))}",
        f"- 总 LOC 变化: {total_loc_delta:+d}",
        f"- 总 CC 变化: {total_cc_delta:+d}",
        f"- 平均自愈轮次: {_format_float(avg_retry)}",
        f"- 平均奖励分: {_format_float(avg_reward)}",
        "",
        "## 战况排行",
        "",
        "| 运行 | 仓库/案例 | 状态 | 自愈 | LOC | CC | 奖励分 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in runs:
        record = item.record
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(record.run_id[-12:]),
                    _md(record.repo_name),
                    _md(_status_cn(record.status)),
                    str(record.self_heal_count),
                    _md(_transition(record.pre_loc, record.post_loc)),
                    _md(_transition(record.pre_cc, record.post_cc)),
                    _format_float(item.reward),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 攻防回合摘录", ""])
    if not runs:
        lines.append("暂无运行记录。")
        return "\n".join(lines)

    for item in runs:
        record = item.record
        lines.extend(
            [
                f"### {record.repo_name} / {record.run_id}",
                "",
                f"- 状态: {_status_cn(record.status)}",
                f"- 工作区: `{item.workspace_path}`",
                f"- LOC: {_transition(record.pre_loc, record.post_loc)}",
                f"- CC: {_transition(record.pre_cc, record.post_cc)}",
                "",
            ]
        )
        messages = build_agent_chat_messages(item.trajectory, limit=8)
        if not messages:
            lines.append("- 暂无 Agent 回合记录。")
            lines.append("")
            continue
        for message in messages:
            reward = "" if message.reward is None else f" 奖励分={message.reward:.2f}"
            lines.append(
                f"- 第 {message.attempt or '-'} 轮 [{message.agent_label} / {message.phase}]{reward}: "
                f"{_compact(message.message, 260)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _transition(before: int | None, after: int | None) -> str:
    if before is None or after is None:
        return "n/a"
    return f"{before} -> {after} ({after - before:+d})"


def _status_cn(status: str) -> str:
    return {"SUCCESS": "成功", "FAILED": "失败"}.get(status, status)


def _percent(part: int, total: int) -> str:
    if total <= 0:
        return "n/a"
    return f"{part / total * 100:.0f}%"


def _average(values: list[int]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _average_float(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _format_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _compact(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
