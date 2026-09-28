from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from refactor_agent.models import RunRecord


STATUS_LABELS = {
    "QUEUED": "排队中",
    "RUNNING": "运行中",
    "CANCEL_REQUESTED": "取消请求中",
    "CANCELLED": "已取消",
    "TIMED_OUT": "已超时",
    "SUCCESS": "成功",
    "FAILED": "失败",
    "DRY_RUN": "试运行完成",
    "REVIEWED": "只读审查完成",
    "MINIMIZER_PROPOSED": "精简者已提交方案",
    "DEFENDER_REVIEWED": "防御者已完成审查",
    "AST_REJECTED": "AST 守卫已拒绝",
    "PYTEST_FAILED": "Pytest 已失败",
    "ADVERSARY_CRITIQUED": "对抗者已完成审查",
    "ADVERSARY_CHALLENGED": "对抗者已发起挑战",
    "ADVERSARY_FAILED": "对抗测试已失败",
    "JUDGE_SCORED": "裁判已评分",
    "DEBATE_CONVERGED": "对抗流程已收敛",
    "CORRUPT": "轨迹已损坏",
}

FAILURE_CATEGORY_LABELS = {
    "TARGETING": "目标定位失败",
    "AST_GUARD": "AST 守卫拒绝",
    "PYTEST": "测试失败",
    "ADVERSARY": "对抗测试失败",
    "MUTATION": "变异测试失败",
    "TIMEOUT": "执行超时",
    "PROVIDER": "模型服务失败",
    "INFRASTRUCTURE": "基础设施失败",
}


@dataclass(frozen=True)
class TaskRow:
    job_id: str
    job_kind: str
    status: str
    repository: str
    issue_number: int | None
    attempts: int
    lease_owner: str | None
    deadline_at: str | None
    remaining_seconds: int | None
    pr_url: str | None
    can_cancel: bool
    can_retry: bool


@dataclass(frozen=True)
class TimelineItem:
    event_id: str
    created_at: str
    label: str
    message: str
    worker_id: str | None


def job_actions(status: str, pr_url: str | None) -> tuple[bool, bool]:
    can_cancel = status in {"QUEUED", "RUNNING"}
    can_retry = status in {"FAILED", "CANCELLED", "TIMED_OUT"} and not pr_url
    return can_cancel, can_retry


def localize_status(status: object) -> str:
    raw = str(status or "UNKNOWN")
    label = STATUS_LABELS.get(raw)
    return f"{label}（{raw}）" if label else f"未知状态（{raw}）"


def localize_job_status(status: object, job_kind: object) -> str:
    raw = str(status or "UNKNOWN")
    kind = str(job_kind or "")
    if raw == "DRY_RUN" and kind in {"SNIPPET", "DASHBOARD_URL"}:
        return "本地验证完成（DRY_RUN）"
    return localize_status(raw)


def format_dashboard_error(status_code: int | None, detail: str) -> str:
    summaries = {
        400: "提交内容格式错误。",
        401: "管理员令牌无效或缺失。",
        403: "仓库不在允许列表中。",
        404: "请求的任务、运行记录或产物不存在。",
        409: "当前状态不允许执行该操作。",
        503: "Worker 当前无法接受 URL 任务。",
    }
    summary = summaries.get(status_code, "无法连接本地 API。" if status_code is None else "仪表盘请求失败。")
    return summary


def build_task_rows(
    jobs: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[TaskRow]:
    current = now or datetime.now(timezone.utc)
    rows = []
    for job in jobs:
        status = str(job.get("status") or "UNKNOWN")
        pr_url = str(job["pr_url"]) if job.get("pr_url") else None
        can_cancel, can_retry = job_actions(status, pr_url)
        if str(job.get("job_kind")) == "GITHUB_WEBHOOK":
            can_cancel = False
            can_retry = False
        deadline_at = str(job["deadline_at"]) if job.get("deadline_at") else None
        rows.append(
            TaskRow(
                job_id=str(job.get("job_id") or ""),
                job_kind=str(job.get("job_kind") or "GITHUB_WEBHOOK"),
                status=status,
                repository=str(job.get("repo_full_name") or ""),
                issue_number=_optional_int(job.get("issue_number")),
                attempts=_optional_int(job.get("attempt_count")) or 0,
                lease_owner=str(job["lease_owner"]) if job.get("lease_owner") else None,
                deadline_at=deadline_at,
                remaining_seconds=_remaining_seconds(deadline_at, current),
                pr_url=pr_url,
                can_cancel=can_cancel,
                can_retry=can_retry,
            )
        )
    return rows


def build_event_timeline(events: list[dict[str, Any]]) -> list[TimelineItem]:
    timeline = []
    for event in events:
        source = event.get("from_status")
        destination = str(event.get("to_status") or event.get("event_type") or "EVENT")
        label = f"{source} -> {destination}" if source else destination
        timeline.append(
            TimelineItem(
                event_id=str(event.get("event_id") or ""),
                created_at=str(event.get("created_at") or ""),
                label=label,
                message=str(event.get("message") or ""),
                worker_id=str(event["worker_id"]) if event.get("worker_id") else None,
            )
        )
    return timeline


def build_task_table_rows(rows: list[TaskRow]) -> list[dict[str, Any]]:
    return [
        {
            "任务 ID": row.job_id,
            "来源": _localize_job_kind(row.job_kind),
            "状态": localize_job_status(row.status, row.job_kind),
            "仓库": row.repository,
            "Issue 编号": row.issue_number if row.issue_number is not None else "-",
            "尝试次数": row.attempts,
            "租约所有者": row.lease_owner,
            "截止时间": row.deadline_at,
            "剩余时间（秒）": row.remaining_seconds,
        }
        for row in rows
    ]


def build_timeline_rows(items: list[TimelineItem]) -> list[dict[str, Any]]:
    return [
        {
            "事件 ID": item.event_id,
            "时间": item.created_at,
            "状态变化": _localize_transition(item.label),
            "消息": item.message,
            "Worker ID": item.worker_id,
        }
        for item in items
    ]


def build_execution_rows(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "轮次": step.get("attempt"),
            "Agent": step.get("agent") or "SYSTEM",
            "状态": localize_status(step.get("status")),
            "消息": str(step.get("message") or "")[:512],
            "奖励分": (step.get("reward") or {}).get("reward") if isinstance(step.get("reward"), dict) else None,
        }
        for step in trajectory
    ]


def build_benchmark_run_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "运行 ID": item.get("run_id"),
            "清单哈希": item.get("manifest_hash"),
            "服务提供方": item.get("provider"),
            "模型": item.get("model"),
            "状态": localize_status(item.get("status")),
            "生成时间": item.get("generated_at"),
        }
        for item in runs
    ]


def build_benchmark_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "案例": item.get("case_name"),
            "仓库": item.get("repository"),
            "状态": localize_status(item.get("status")),
            "失败类别": _localize_failure_category(item.get("failure_category")),
            "Token 数": item.get("total_tokens", 0),
            "成本（USD）": item.get("cost_usd", 0),
            "尝试次数": item.get("attempts", 0),
        }
        for item in cases
    ]


def _localize_transition(label: str) -> str:
    return " -> ".join(localize_status(part.strip()) for part in label.split(" -> "))


def _localize_failure_category(category: object) -> str:
    if not category:
        return "-"
    raw = str(category)
    label = FAILURE_CATEGORY_LABELS.get(raw)
    return f"{label}（{raw}）" if label else f"未知类别（{raw}）"


def _localize_job_kind(job_kind: str) -> str:
    return {
        "GITHUB_WEBHOOK": "遗留任务（已禁用）",
        "DASHBOARD_URL": "仪表盘 URL",
    }.get(job_kind, f"未知来源（{job_kind}）")


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _remaining_seconds(deadline_at: str | None, now: datetime) -> int | None:
    if not deadline_at:
        return None
    try:
        deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        return None
    return max(int((deadline - now).total_seconds()), 0)


@dataclass(frozen=True)
class DashboardRun:
    record: RunRecord
    workspace_path: Path
    loc_delta: int | None
    cc_delta: int | None
    loc_reduction_percent: float | None
    cc_reduction_percent: float | None
    reward: float | None
    trajectory: list[dict[str, Any]]
    candidate_files: list[Path]


@dataclass(frozen=True)
class DashboardChatMessage:
    attempt: int | None
    agent: str
    agent_label: str
    phase: str
    message: str
    side: str
    tone: str
    reward: float | None = None


def load_trajectory(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    steps: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            steps.append(json.loads(line))
        except json.JSONDecodeError:
            steps.append({"attempt": None, "status": "CORRUPT", "message": line})
    return steps


def build_agent_chat_messages(
    trajectory: list[dict[str, Any]],
    limit: int = 40,
) -> list[DashboardChatMessage]:
    messages: list[DashboardChatMessage] = []
    for step in trajectory:
        message = str(step.get("message") or "").strip()
        if not message:
            continue
        agent = str(step.get("agent") or "SYSTEM")
        status = str(step.get("status") or "-")
        messages.append(
            DashboardChatMessage(
                attempt=_optional_int(step.get("attempt")),
                agent=agent,
                agent_label=_agent_label(agent),
                phase=_phase_label(status),
                message=_compact_text(message, 560),
                side=_agent_side(agent),
                tone=_agent_tone(agent, status),
                reward=_step_reward(step),
            )
        )
    if limit <= 0:
        return messages
    return messages[-limit:]


def build_overview_chart_rows(runs: list[DashboardRun]) -> list[dict[str, Any]]:
    return [
        {
            "运行": item.record.run_id[-8:],
            "LOC 变化": item.loc_delta or 0,
            "CC 变化": item.cc_delta or 0,
            "奖励分": item.reward or 0,
        }
        for item in reversed(runs)
    ]


def build_before_after_rows(item: DashboardRun) -> list[dict[str, Any]]:
    return [
        {"指标": "LOC", "重构前": item.record.pre_loc or 0, "重构后": item.record.post_loc or 0},
        {"指标": "CC", "重构前": item.record.pre_cc or 0, "重构后": item.record.post_cc or 0},
    ]


def _delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return after - before


def _reduction_percent(before: int | None, after: int | None) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return (before - after) / before * 100


def _last_reward(trajectory: list[dict[str, Any]]) -> float | None:
    for step in reversed(trajectory):
        reward = step.get("reward")
        if isinstance(reward, dict) and isinstance(reward.get("reward"), int | float):
            return float(reward["reward"])
    return None


def _candidate_files(workspace: Path) -> list[Path]:
    if not workspace.is_dir():
        return []
    ignored_parts = {"__pycache__", ".adversary_tests"}
    files = [
        path
        for path in workspace.rglob("*.py")
        if not any(part in ignored_parts for part in path.parts)
    ]
    return sorted(files)[:10]


def _average(values: list[int]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _average_float(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _format_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}"


def _format_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def _status_label(status: str) -> str:
    return localize_status(status)


def _phase_label(status: str | None) -> str:
    labels = {
        "MINIMIZER_PROPOSED": "Minimizer 提案",
        "DEFENDER_REVIEWED": "Defender 审查",
        "AST_REJECTED": "AST 守卫拦截",
        "PYTEST_FAILED": "Pytest 失败",
        "ADVERSARY_CRITIQUED": "Adversary 红队审查",
        "ADVERSARY_CHALLENGED": "Adversary 攻击",
        "ADVERSARY_FAILED": "对抗测试击穿",
        "JUDGE_SCORED": "Judge 评分",
        "DEBATE_CONVERGED": "对抗收敛",
        "SUCCESS": "裁决通过",
        "FAILED": "运行失败",
        "CORRUPT": "轨迹损坏",
    }
    return labels.get(status or "", status or "-")


def _compact_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _step_reward(step: dict[str, Any]) -> float | None:
    reward = step.get("reward")
    if not isinstance(reward, dict):
        return None
    value = reward.get("reward")
    if isinstance(value, int | float):
        return float(value)
    return None


def _agent_side(agent: str) -> str:
    return {
        "MINIMIZER": "left",
        "DEFENDER": "right",
        "ADVERSARY": "right",
        "JUDGE": "center",
    }.get(agent, "center")


def _agent_tone(agent: str, status: str) -> str:
    if status in {"FAILED", "PYTEST_FAILED", "AST_REJECTED", "ADVERSARY_FAILED"}:
        return "adversary"
    return {
        "MINIMIZER": "minimizer",
        "DEFENDER": "defender",
        "ADVERSARY": "adversary",
        "JUDGE": "judge",
    }.get(agent, "system")


def _agent_label(agent: str | None) -> str:
    labels = {
        "MINIMIZER": "精简狂魔",
        "DEFENDER": "防御大师",
        "ADVERSARY": "测试刺客",
        "JUDGE": "董事会法官",
        "SYSTEM": "系统",
    }
    return labels.get(agent or "", agent or "-")


def _metadata_summary(metadata: Any) -> str:
    if not isinstance(metadata, dict) or not metadata:
        return "-"
    parts = []
    for key, value in metadata.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:.2f}")
        elif isinstance(value, (str, int, bool)):
            parts.append(f"{key}={value}")
    return ", ".join(parts[:4]) if parts else "-"


def _table_row(item: DashboardRun) -> dict[str, Any]:
    return {
        "运行 ID": item.record.run_id,
        "仓库/案例": item.record.repo_name,
        "状态": _status_label(item.record.status),
        "自愈轮次": item.record.self_heal_count,
        "LOC": f"{item.record.pre_loc} -> {item.record.post_loc}",
        "LOC 变化": item.loc_delta,
        "LOC 压缩率": _format_percent(item.loc_reduction_percent),
        "CC": f"{item.record.pre_cc} -> {item.record.post_cc}",
        "CC 变化": item.cc_delta,
        "CC 压缩率": _format_percent(item.cc_reduction_percent),
        "奖励分": item.reward,
        "候选文件数": len(item.candidate_files),
    }


def _record_summary(item: DashboardRun) -> str:
    return "\n".join(
        [
            f"状态: {_status_label(item.record.status)}",
            f"运行 ID: {item.record.run_id}",
            f"仓库/案例: {item.record.repo_name}",
            f"自愈轮次: {item.record.self_heal_count}",
            f"LOC: {item.record.pre_loc} -> {item.record.post_loc} ({_format_delta(item.loc_delta)})",
            f"LOC 压缩率: {_format_percent(item.loc_reduction_percent)}",
            f"圈复杂度: {item.record.pre_cc} -> {item.record.post_cc} ({_format_delta(item.cc_delta)})",
            f"CC 压缩率: {_format_percent(item.cc_reduction_percent)}",
            f"奖励分: {_format_float(item.reward)}",
            f"错误: {item.record.error_message or '-'}",
        ]
    )
