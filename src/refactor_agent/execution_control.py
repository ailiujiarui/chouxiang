from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone


class ExecutionStopped(RuntimeError):
    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        super().__init__(f"{reason} at checkpoint {stage}")


class ExecutionCancelled(ExecutionStopped):
    def __init__(self, stage: str) -> None:
        super().__init__(stage, "execution cancelled")


class ExecutionDeadlineExceeded(ExecutionStopped):
    def __init__(self, stage: str) -> None:
        super().__init__(stage, "execution deadline exceeded")


@dataclass(frozen=True, slots=True)
class ExecutionControl:
    deadline_at: datetime
    is_cancel_requested: Callable[[], bool] = field(default=lambda: False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")

    def remaining_seconds(self) -> float:
        return max((self.deadline_at - self.clock()).total_seconds(), 0.0)

    def checkpoint(self, stage: str) -> None:
        if self.is_cancel_requested():
            raise ExecutionCancelled(stage)
        if self.remaining_seconds() <= 0:
            raise ExecutionDeadlineExceeded(stage)

    def bounded_timeout(self, component_timeout: float, stage: str) -> float:
        checkpoint = f"before-{stage}"
        self.checkpoint(checkpoint)
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise ExecutionDeadlineExceeded(checkpoint)
        return min(float(component_timeout), remaining)
