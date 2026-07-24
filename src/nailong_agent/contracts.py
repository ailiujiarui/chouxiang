from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nailong_agent.events import PopupDecision, Sensitivity, utc_now


class PetSituation(StrEnum):
    """Normalized situations understood by the personality decision graph."""

    CODING = "coding"
    DEBUGGING = "debugging"
    TEST_FAILED = "test_failed"
    TEST_SUCCEEDED = "test_succeeded"
    COMPILE_SUCCEEDED = "compile_succeeded"
    LONG_WORK = "long_work"
    IDLE = "idle"
    MEETING = "meeting"
    ENTERTAINMENT = "entertainment"
    UNKNOWN = "unknown"


PetSignalSource = Literal["window", "process", "idle", "ide", "analysis", "user", "system"]
PetClassificationSource = Literal["collector", "rules", "classifier", "llm", "analysis", "user"]
RecentMessage = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class RedactedActivitySignal(BaseModel):
    """Minimal event data allowed to cross into the personality graph.

    The contract intentionally has no fields for raw code, clipboard contents,
    screenshots, terminal text, full window titles, credentials, or arbitrary
    metadata. ``extra="forbid"`` makes accidental additions fail closed.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    occurred_at: datetime = Field(default_factory=utc_now)
    source: PetSignalSource
    application_id: str = Field(min_length=1, max_length=128)
    activity_hint: str | None = Field(default=None, max_length=100)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    redacted_summary: str | None = Field(default=None, max_length=500)
    sensitivity: Sensitivity = "public"

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class PetClassificationHint(BaseModel):
    """Optional upstream classification without raw evidence or source text."""

    model_config = ConfigDict(extra="forbid")

    situation: PetSituation
    confidence: float = Field(ge=0.0, le=1.0)
    classifier: PetClassificationSource


class PetDecisionContext(BaseModel):
    """Non-content context used later by interruption policy nodes."""

    model_config = ConfigDict(extra="forbid")

    now: datetime = Field(default_factory=utc_now)
    paused: bool = False
    quiet_hours_active: bool = False
    is_fullscreen: bool = False
    is_meeting: bool = False
    daily_popup_count: int = Field(default=0, ge=0)
    last_popup_at: datetime | None = None
    recent_messages: list[RecentMessage] = Field(default_factory=list, max_length=20)

    @field_validator("now")
    @classmethod
    def require_now_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return value

    @field_validator("last_popup_at")
    @classmethod
    def require_last_popup_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("last_popup_at must be timezone-aware")
        return value


class PetDecisionInput(BaseModel):
    """Public input boundary for one personality decision."""

    model_config = ConfigDict(extra="forbid")

    signal: RedactedActivitySignal
    classification: PetClassificationHint | None = None
    context: PetDecisionContext = Field(default_factory=PetDecisionContext)


PetDecisionOutput: TypeAlias = PopupDecision
