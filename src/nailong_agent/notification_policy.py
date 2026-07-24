from __future__ import annotations

from dataclasses import dataclass
import random

from nailong_agent.events import NotificationKind
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType


@dataclass(frozen=True)
class NotificationCandidate:
    kind: NotificationKind
    message: str
    priority: str = "normal"
    terminal: bool = False


class NotificationPolicy:
    """Pure event-to-message policy; persistence owns timing and deduplication."""

    def __init__(
        self,
        *,
        minimum_cooldown_seconds: int = 5 * 60,
        maximum_cooldown_seconds: int = 15 * 60,
        rng: random.Random | None = None,
    ) -> None:
        if minimum_cooldown_seconds < 0 or maximum_cooldown_seconds < minimum_cooldown_seconds:
            raise ValueError("invalid notification cooldown range")
        self.minimum_cooldown_seconds = minimum_cooldown_seconds
        self.maximum_cooldown_seconds = maximum_cooldown_seconds
        self._rng = rng or random.Random()

    def cooldown_seconds(self) -> int:
        return self._rng.randint(self.minimum_cooldown_seconds, self.maximum_cooldown_seconds)

    def candidate_for(self, event: AnalysisEvent) -> NotificationCandidate | None:
        event_type = event.event_type
        if event_type == AnalysisEventType.TASK_STARTED:
            return NotificationCandidate(
                NotificationKind.ENCOURAGEMENT,
                "任务已接手。我会盯住测试、对抗验证和最终裁决。",
            )
        if event_type == AnalysisEventType.PHASE_STARTED and event.attempt >= 2 and event.phase == "minimizer":
            return NotificationCandidate(
                NotificationKind.LIGHT_TEASE,
                f"第 {event.attempt} 轮了，这段分支还挺会躲。继续收拾它。",
                priority="low",
            )
        if event_type == AnalysisEventType.AST_REJECTED:
            return NotificationCandidate(
                NotificationKind.DEBUG_HINT,
                "AST 守卫把这版拦下了；问题还可恢复，正在换个解法。",
            )
        if event_type == AnalysisEventType.PYTEST_FAILED:
            return NotificationCandidate(
                NotificationKind.DEBUG_HINT,
                "pytest 抓到了回归。好消息是，它在提交前就暴露了。",
            )
        if event_type == AnalysisEventType.ADVERSARY_FAILED:
            return NotificationCandidate(
                NotificationKind.DEBUG_HINT,
                "对抗测试找到了反例，这版还得再磨一轮。",
            )
        if event_type == AnalysisEventType.PYTEST_PASSED:
            return NotificationCandidate(
                NotificationKind.PYTEST_CELEBRATION,
                "pytest 已经全绿。先别急着开香槟，我还要等最终裁决。",
            )
        if event_type == AnalysisEventType.FINAL_VERDICT_PASSED:
            return NotificationCandidate(
                NotificationKind.FINAL_CELEBRATION,
                "最终裁决通过：证据链闭合，这次可以放心庆祝。",
                priority="high",
                terminal=True,
            )
        if event_type == AnalysisEventType.FINAL_VERDICT_FAILED:
            return NotificationCandidate(
                NotificationKind.TERMINAL_FAILURE,
                "最终裁决未通过：这版没拿到足够证据，先别合并。",
                priority="high",
                terminal=True,
            )
        if event_type == AnalysisEventType.TASK_COMPLETED:
            return NotificationCandidate(
                NotificationKind.FINAL_CELEBRATION,
                "任务已完成，结果和报告已经准备好。",
                priority="high",
                terminal=True,
            )
        if event_type == AnalysisEventType.TASK_TIMED_OUT:
            return NotificationCandidate(
                NotificationKind.TERMINAL_FAILURE,
                "任务超时结束。至少它没有无限占着跑道。",
                priority="high",
                terminal=True,
            )
        if event_type == AnalysisEventType.TASK_CANCELLED:
            return NotificationCandidate(
                NotificationKind.TERMINAL_FAILURE,
                "任务已取消，我把这条链路收住了。",
                terminal=True,
            )
        if event_type == AnalysisEventType.TASK_FAILED:
            return NotificationCandidate(
                NotificationKind.TERMINAL_FAILURE,
                "任务未完成：链路遇到不可恢复问题，可打开报告查看下一步。",
                priority="high",
                terminal=True,
            )
        return None

    @staticmethod
    def long_task_candidate() -> NotificationCandidate:
        return NotificationCandidate(
            NotificationKind.LONG_TASK_REMINDER,
            "这个任务已跑过预计时限的三分之一，还在工作，不是卡死。",
        )

    @staticmethod
    def quiet_summary_candidate(count: int) -> NotificationCandidate:
        return NotificationCandidate(
            NotificationKind.QUIET_MODE_SUMMARY,
            f"全天免打扰期间有 {count} 个任务到达终态。结果已经归拢好。",
            priority="high",
            terminal=True,
        )
