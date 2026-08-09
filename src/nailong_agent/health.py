from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

from pydantic import BaseModel, ConfigDict, Field

from nailong_agent.events import ActivityEvent, ActivityType, NotificationStatus, PetPreferences
from nailong_agent.privacy import PrivacyConsent


_APPLICATION_CATEGORIES = {
    "browser",
    "code",
    "explorer",
    "game",
    "ide",
    "other",
    "system",
    "terminal",
}
_ACTIVITY_TYPES = {activity.value for activity in ActivityType}
_ERROR_CODES = {
    "delivery_publish_failed",
    "event_bus_rejected",
    "listener_error",
    "renderer_error",
    "runtime_error",
}
_DELIVERY_OUTCOMES = {"dismissed", "failed", "queued_for_render", "shown"}
_SILENCE_REASON_LABELS = {
    "activity_listener_disabled": "活动监听已关闭",
    "awaiting_activity": "正在等待新的活动信号",
    "cooldown": "通知仍在冷却期",
    "daily_budget_exhausted": "今日弹窗预算已用完",
    "do_not_disturb": "全天免打扰已开启",
    "fullscreen_blocked": "全屏正在阻止普通通知",
    "game_tease_disabled": "游戏吐槽开关未开启",
    "game_tease_waiting": "游戏吐槽正在等待触发",
    "listener_error": "活动监听发生错误",
    "listener_stopped": "活动监听未运行",
    "low_confidence": "活动识别置信度不足",
    "manual_pause": "监听已手动暂停",
    "pending_delivery": "通知正在等待投递",
    "privacy_not_authorized": "尚未授权本地活动识别",
    "quiet_by_policy": "当前活动按策略保持安静",
    "scheduled_do_not_disturb": "当前处于定时免打扰",
}


class NailongHealthSnapshot(BaseModel):
    """Privacy-minimized runtime diagnostics safe for local display."""

    model_config = ConfigDict(extra="forbid")

    listener_running: bool = False
    last_event_at: datetime | None = None
    last_application_category: str | None = None
    last_activity_type: str | None = None
    last_classification_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    pending_count: int = Field(default=0, ge=0)
    displaying_count: int = Field(default=0, ge=0)
    remaining_daily_budget: int = Field(default=0, ge=0)
    fullscreen_blocked: bool = False
    silence_reason: str = "listener_stopped"
    last_error_code: str | None = None
    last_delivery_outcome: str | None = None

    def redacted_summary(self) -> str:
        event_time = self.last_event_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if self.last_event_at else "无"
        confidence = (
            f"{self.last_classification_confidence:.2f}"
            if self.last_classification_confidence is not None
            else "无"
        )
        silence_label = _SILENCE_REASON_LABELS.get(
            self.silence_reason,
            "未知状态",
        )
        return "\n".join(
            (
                f"监听运行：{'是' if self.listener_running else '否'}",
                f"最近活动：{event_time}",
                f"应用分类：{self.last_application_category or '无'}",
                f"活动类型：{self.last_activity_type or '无'}",
                f"分类置信度：{confidence}",
                f"待处理通知：{self.pending_count}",
                f"正在显示：{self.displaying_count}",
                f"今日剩余预算：{self.remaining_daily_budget}",
                f"全屏门禁：{'阻止普通通知' if self.fullscreen_blocked else '未阻止'}",
                f"当前静默原因：{silence_label}（{self.silence_reason}）",
                f"最近错误码：{self.last_error_code or '无'}",
                f"最近投递结果：{self.last_delivery_outcome or '无'}",
            )
        )


class NailongHealthMonitor:
    def __init__(self) -> None:
        self._lock = Lock()
        self._state = NailongHealthSnapshot()

    def record_listener(self, *, running: bool, error_code: str | None = None) -> None:
        safe_error = (
            _safe_value(error_code, _ERROR_CODES, "runtime_error")
            if error_code is not None
            else None
        )
        self._update(listener_running=running, last_error_code=safe_error)

    def record_activity(self, event: ActivityEvent) -> None:
        self._update(
            last_event_at=event.occurred_at,
            last_application_category=_safe_value(
                event.application_id,
                _APPLICATION_CATEGORIES,
                "other",
            ),
            last_activity_type=_safe_value(
                event.activity.value,
                _ACTIVITY_TYPES,
                ActivityType.UNKNOWN.value,
            ),
            last_classification_confidence=event.confidence,
        )

    def record_classification(
        self,
        *,
        application_category: str,
        activity_type: str,
        confidence: float,
    ) -> None:
        self._update(
            last_application_category=_safe_value(
                application_category,
                _APPLICATION_CATEGORIES,
                "other",
            ),
            last_activity_type=_safe_value(
                activity_type,
                _ACTIVITY_TYPES,
                ActivityType.UNKNOWN.value,
            ),
            last_classification_confidence=confidence,
        )

    def record_delivery(
        self,
        *,
        fullscreen_blocked: bool | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
    ) -> None:
        updates: dict[str, object] = {}
        if fullscreen_blocked is not None:
            updates["fullscreen_blocked"] = fullscreen_blocked
        if outcome is not None:
            updates["last_delivery_outcome"] = _safe_value(
                outcome,
                _DELIVERY_OUTCOMES,
                "failed",
            )
        if error_code is not None:
            updates["last_error_code"] = _safe_value(
                error_code,
                _ERROR_CODES,
                "runtime_error",
            )
        self._update(**updates)

    def record_error(self, error_code: str) -> None:
        self._update(
            last_error_code=_safe_value(error_code, _ERROR_CODES, "runtime_error")
        )

    def snapshot(
        self,
        *,
        preferences: PetPreferences | None = None,
        status: NotificationStatus | None = None,
        consent: PrivacyConsent | None = None,
        fullscreen_blocked: bool | None = None,
        now: datetime | None = None,
    ) -> NailongHealthSnapshot:
        with self._lock:
            state = self._state
        if fullscreen_blocked is None:
            fullscreen_blocked = state.fullscreen_blocked
        updates: dict[str, object] = {"fullscreen_blocked": fullscreen_blocked}
        if status is not None:
            updates.update(
                pending_count=status.pending_count,
                displaying_count=status.displaying_count,
                remaining_daily_budget=status.remaining_daily_popup_budget,
            )
        current = state.model_copy(update=updates)
        return current.model_copy(
            update={
                "silence_reason": _silence_reason(
                    current,
                    preferences=preferences,
                    status=status,
                    consent=consent,
                    now=now or datetime.now(timezone.utc),
                )
            }
        )

    def _update(self, **updates: object) -> None:
        if not updates:
            return
        with self._lock:
            self._state = self._state.model_copy(update=updates)


def _silence_reason(
    snapshot: NailongHealthSnapshot,
    *,
    preferences: PetPreferences | None,
    status: NotificationStatus | None,
    consent: PrivacyConsent | None,
    now: datetime,
) -> str:
    if preferences is not None and not preferences.activity_listener_enabled:
        return "activity_listener_disabled"
    if snapshot.last_error_code == "listener_error":
        return "listener_error"
    if not snapshot.listener_running:
        return "listener_stopped"
    if consent is not None and not consent.activity_collection_enabled:
        return "privacy_not_authorized"
    if preferences is not None and preferences.manual_pause_enabled:
        return "manual_pause"
    if status is not None and status.do_not_disturb:
        return "do_not_disturb"
    if status is not None and status.scheduled_do_not_disturb:
        return "scheduled_do_not_disturb"
    if status is not None and status.remaining_daily_popup_budget == 0:
        return "daily_budget_exhausted"
    if snapshot.last_application_category == "game" and preferences is not None and not preferences.game_tease_enabled:
        return "game_tease_disabled"
    if snapshot.last_classification_confidence is not None and snapshot.last_classification_confidence < 0.65:
        return "low_confidence"
    if status is not None and status.pending_count > 0:
        return "pending_delivery"
    if status is not None and status.next_regular_at is not None and now < status.next_regular_at:
        return "cooldown"
    game_tease_enabled = bool(preferences is not None and preferences.game_tease_enabled)
    if snapshot.fullscreen_blocked and not (
        snapshot.last_application_category == "game" and game_tease_enabled
    ):
        return "fullscreen_blocked"
    if snapshot.last_application_category == "game" and game_tease_enabled:
        return "game_tease_waiting"
    if snapshot.last_event_at is None:
        return "awaiting_activity"
    return "quiet_by_policy"


def _safe_value(value: str, allowed: set[str], fallback: str) -> str:
    return value if value in allowed else fallback
