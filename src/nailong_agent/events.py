from __future__ import annotations

from datetime import datetime, time, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

Sensitivity = Literal["public", "private", "blocked"]
ActivitySource = Literal["window", "process", "idle", "ide"]


class ActivityType(StrEnum):
    CODING = "coding"
    DEBUGGING = "debugging"
    TEST_FAILED = "test_failed"
    TEST_SUCCEEDED = "test_succeeded"
    COMPILE_SUCCEEDED = "compile_succeeded"
    READING = "reading"
    WRITING = "writing"
    MEETING = "meeting"
    GAMING = "gaming"
    MEDIA = "media"
    IDLE = "idle"
    UNKNOWN = "unknown"


class PetExpression(StrEnum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    LAUGH = "laugh"
    CONCERNED = "concerned"
    SLEEPY = "sleepy"


class PetState(BaseModel):
    expression: PetExpression = PetExpression.NEUTRAL
    bubble_text: str | None = Field(default=None, max_length=500)
    bubble_visible: bool = False
    bubble_seconds: int = Field(default=5, ge=0, le=300)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventEnvelope(BaseModel):
    """Serializable envelope used at process boundaries and in the event bus."""

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str
    occurred_at: datetime = Field(default_factory=utc_now)
    source: str
    schema_version: int = Field(default=1, ge=1)
    sensitivity: Sensitivity = "public"
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: BaseModel, *, source: str, sensitivity: Sensitivity = "public") -> "EventEnvelope":
        payload_data = payload.model_dump(mode="json")
        return cls(
            event_id=getattr(payload, "event_id", uuid4().hex),
            event_type=payload.__class__.__name__,
            occurred_at=getattr(payload, "occurred_at", utc_now()),
            source=source,
            sensitivity=sensitivity,
            payload=payload_data,
        )


class RawActivitySignal(BaseModel):
    """Ephemeral collector input. It must never be persisted or published."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=utc_now)
    source: ActivitySource
    application_id: str = Field(min_length=1)
    window_title_summary: str | None = None
    activity_hint: str | None = None
    activity: ActivityType = ActivityType.UNKNOWN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sensitivity: Sensitivity = "public"
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)


class ActivityEvent(BaseModel):
    """Privacy-minimized activity contract used outside the collector boundary."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=utc_now)
    source: ActivitySource
    application_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    activity: ActivityType
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str | None = Field(default=None, max_length=240)
    sensitivity: Sensitivity = "public"

    @field_validator("occurred_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    def envelope(self) -> EventEnvelope:
        return EventEnvelope.from_payload(self, source=self.source, sensitivity=self.sensitivity)


class ActivityWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_started_at: datetime
    window_ended_at: datetime
    dominant_application: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    dominant_activity: ActivityType
    confidence: float = Field(ge=0.0, le=1.0)
    event_count: int = Field(ge=1)
    duplicate_count: int = Field(default=0, ge=0)
    summary: str | None = Field(default=None, max_length=240)
    sensitivity: Literal["public"] = "public"

    @field_validator("window_started_at", "window_ended_at")
    @classmethod
    def validate_window_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)


class ActivitySnapshot(BaseModel):
    window_started_at: datetime
    window_ended_at: datetime
    dominant_application: str | None = None
    normalized_signals: list[str] = Field(default_factory=list)
    idle_seconds: int = Field(default=0, ge=0)
    is_fullscreen: bool = False
    is_meeting_likely: bool = False
    sensitivity: Sensitivity = "public"

    def envelope(self) -> EventEnvelope:
        return EventEnvelope.from_payload(self, source="context_aggregator", sensitivity=self.sensitivity)


class ActivityClassification(BaseModel):
    activity: ActivityType
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    classifier: Literal["rules", "lightweight", "llm"] = "rules"

    def envelope(self) -> EventEnvelope:
        return EventEnvelope.from_payload(self, source="activity_classifier")


class PersonalityResponseProposal(BaseModel):
    persona_version: str = Field(min_length=1)
    emotion: Literal["cheerful", "curious", "concerned", "sleepy", "celebrating", "neutral"]
    message: str = Field(min_length=1, max_length=500)
    intent: Literal["encourage", "remind", "celebrate", "ask", "stay_silent"]
    priority: Literal["low", "normal", "high"] = "normal"
    expires_in_seconds: int = Field(default=300, ge=0, le=86_400)

    def envelope(self) -> EventEnvelope:
        return EventEnvelope.from_payload(self, source="personality_agent")


class PopupDecision(BaseModel):
    action: Literal["show", "defer", "drop"]
    reason: str = Field(min_length=1)
    message: str | None = Field(default=None, max_length=500)
    priority: Literal["low", "normal", "high"] = "normal"
    display_seconds: int = Field(default=5, ge=0, le=300)
    dedupe_key: str | None = None

    def envelope(self) -> EventEnvelope:
        return EventEnvelope.from_payload(self, source="popup_policy")


class NotificationKind(StrEnum):
    ENCOURAGEMENT = "encouragement"
    LIGHT_TEASE = "light_tease"
    DEBUG_HINT = "debug_hint"
    PYTEST_CELEBRATION = "pytest_celebration"
    FINAL_CELEBRATION = "final_celebration"
    LONG_TASK_REMINDER = "long_task_reminder"
    TERMINAL_FAILURE = "terminal_failure"
    QUIET_MODE_SUMMARY = "quiet_mode_summary"


class NotificationIntent(BaseModel):
    notification_id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str = Field(min_length=1)
    kind: NotificationKind
    message: str = Field(min_length=1, max_length=500)
    priority: Literal["low", "normal", "high"] = "normal"
    terminal: bool = False
    dedupe_key: str = Field(min_length=1)
    source_event_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    available_at: datetime = Field(default_factory=utc_now)


class PetPreferences(BaseModel):
    activity_listener_enabled: bool = True
    manual_pause_enabled: bool = False
    do_not_disturb_start: time | None = None
    do_not_disturb_end: time | None = None
    minimum_cooldown_seconds: int = Field(default=5 * 60, ge=0)
    maximum_cooldown_seconds: int = Field(default=15 * 60, ge=0)
    maximum_popups_per_day: int = Field(default=12, ge=0)
    personality_intensity: Literal["LOW", "STANDARD", "HIGH"] = "STANDARD"


class PetApplicationRule(BaseModel):
    application_id: str = Field(min_length=1, max_length=64)
    rule: Literal["allow", "block"]


class NotificationStatus(BaseModel):
    do_not_disturb: bool
    last_consumed_sequence: int = Field(ge=0)
    next_regular_at: datetime | None = None
    last_popup_started_at: datetime | None = None
    pending_count: int = Field(ge=0)
    suppressed_terminal_count: int = Field(ge=0)
    manual_pause_enabled: bool = False
    scheduled_do_not_disturb: bool = False
    remaining_daily_popup_budget: int = Field(default=0, ge=0)


class NotificationIngestReceipt(BaseModel):
    accepted: bool
    duplicate: bool = False
    notification_id: str | None = None
    reason: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("activity timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
