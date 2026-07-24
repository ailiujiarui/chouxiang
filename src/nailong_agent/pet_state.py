from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

from nailong_agent.contracts import (
    PetClassificationHint,
    PetClassificationSource,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PetPersonalityResponse,
    PersonalityScenario,
    RedactedActivitySignal,
)


class PetEmotion(StrEnum):
    CHEERFUL = "cheerful"
    CURIOUS = "curious"
    CONCERNED = "concerned"
    SLEEPY = "sleepy"
    CELEBRATING = "celebrating"
    NEUTRAL = "neutral"


class PersonalityIntensity(StrEnum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class PetPersonalityState(BaseModel):
    """Durable, non-content state for restoring the pet after a restart."""

    emotion: PetEmotion = PetEmotion.NEUTRAL
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("updated_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("personality state timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


PolicyAction = Literal["show", "drop"]


class PetGraphState(TypedDict, total=False):
    """State owned only by the desktop personality decision graph."""

    decision_input: PetDecisionInput
    signal: RedactedActivitySignal
    context: PetDecisionContext
    provided_classification: PetClassificationHint | None
    scenario: PersonalityScenario
    classification_confidence: float
    classification_source: PetClassificationSource
    emotion: PetEmotion
    response: PetPersonalityResponse
    policy_action: PolicyAction
    policy_reason: str
    output: PetDecisionOutput
    llm_used: bool
    llm_error: str | None
    node_trace: list[str]
