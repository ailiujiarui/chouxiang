from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


STATUS_LABELS = {
    "QUEUED": "排队中",
    "RUNNING": "运行中",
    "CANCEL_REQUESTED": "取消请求中",
    "CANCELLED": "已取消",
    "TIMED_OUT": "已超时",
    "SUCCESS": "成功",
    "FAILED": "失败",
    "DRY_RUN": "试运行完成",
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
    clean_detail = detail.strip()
    return f"{summary}详细信息：{clean_detail}" if clean_detail else summary


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
            "状态": localize_status(row.status),
            "仓库": row.repository,
            "Issue 编号": row.issue_number if row.issue_number is not None else "-",
            "尝试次数": row.attempts,
            "租约所有者": row.lease_owner,
            "截止时间": row.deadline_at,
            "剩余时间（秒）": row.remaining_seconds,
            "PR URL": row.pr_url,
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
        "GITHUB_WEBHOOK": "GitHub Webhook",
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
