from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

Sensitivity = Literal["public", "private", "blocked"]


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


class ActivityEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=utc_now)
    source: Literal["window", "process", "idle", "ide"]
    application_id: str = Field(min_length=1)
    window_title_summary: str | None = None
    activity_hint: str | None = None
    sensitivity: Sensitivity = "public"
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    def envelope(self) -> EventEnvelope:
        return EventEnvelope.from_payload(self, source=self.source, sensitivity=self.sensitivity)


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
    activity: Literal[
        "coding",
        "debugging",
        "reading",
        "writing",
        "meeting",
        "gaming",
        "media",
        "idle",
        "unknown",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    classifier: Literal["rules", "llm"] = "rules"

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
