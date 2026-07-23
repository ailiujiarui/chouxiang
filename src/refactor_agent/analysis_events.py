from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import math
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class AnalysisEventType(StrEnum):
    TASK_QUEUED = "TASK_QUEUED"
    TASK_STARTED = "TASK_STARTED"
    PHASE_STARTED = "PHASE_STARTED"
    AST_REJECTED = "AST_REJECTED"
    PYTEST_PASSED = "PYTEST_PASSED"
    PYTEST_FAILED = "PYTEST_FAILED"
    ADVERSARY_PASSED = "ADVERSARY_PASSED"
    ADVERSARY_FAILED = "ADVERSARY_FAILED"
    FINAL_VERDICT_PASSED = "FINAL_VERDICT_PASSED"
    FINAL_VERDICT_FAILED = "FINAL_VERDICT_FAILED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_TIMED_OUT = "TASK_TIMED_OUT"
    TASK_CANCELLED = "TASK_CANCELLED"


SafeMetric = str | int | float | bool | None

_SAFE_METRIC_KEYS = {
    "job_status",
    "returncode",
    "duration_seconds",
    "generated_tests",
    "pre_loc",
    "post_loc",
    "pre_cc",
    "post_cc",
    "self_heal_count",
    "reward",
    "mutation_kill_rate",
}
_SAFE_JOB_STATUSES = {
    "QUEUED",
    "RUNNING",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "TIMED_OUT",
    "SUCCESS",
    "FAILED",
    "DRY_RUN",
}


class AnalysisEvent(BaseModel):
    """A sanitized project fact that may cross into the desktop process."""

    sequence: int | None = None
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    schema_version: int = Field(default=1, ge=1)
    event_type: AnalysisEventType
    task_id: str = Field(min_length=1)
    run_id: str | None = None
    source: Literal["worker", "orchestrator", "system"]
    phase: str | None = None
    attempt: int = Field(default=0, ge=0)
    evidence_level: str | None = None
    error_category: str | None = None
    recoverable: bool | None = None
    deadline_at: datetime | None = None
    safe_metrics: dict[str, SafeMetric] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sensitivity: Literal["public", "private", "blocked"] = "public"

    @field_validator("safe_metrics")
    @classmethod
    def validate_safe_metrics(cls, metrics: dict[str, SafeMetric]) -> dict[str, SafeMetric]:
        unknown = sorted(set(metrics) - _SAFE_METRIC_KEYS)
        if unknown:
            raise ValueError(f"unsupported safe metric keys: {', '.join(unknown)}")
        for key, value in metrics.items():
            if isinstance(value, str) and (key != "job_status" or value not in _SAFE_JOB_STATUSES):
                raise ValueError(f"string value is not allowed for safe metric: {key}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"non-finite value is not allowed for safe metric: {key}")
        return metrics


class PublishReceipt(BaseModel):
    accepted: bool
    duplicate: bool = False
    sequence: int | None = None
    reason: str


class AnalysisEventSink(Protocol):
    def emit(self, event: AnalysisEvent) -> PublishReceipt: ...


class NullAnalysisEventSink:
    def emit(self, event: AnalysisEvent) -> PublishReceipt:
        return PublishReceipt(accepted=True, reason="notification_sink_disabled")
