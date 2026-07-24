from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nailong_agent.events import PersonalityResponseProposal, Sensitivity, utc_now


class PersonalityScenario(StrEnum):
    """Internal scenarios used only to select personality behavior."""

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
PetClassificationSource = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
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
    redacted_summary: str | None = Field(default=None, max_length=500)
    sensitivity: Sensitivity = "public"

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class PetClassificationHint(BaseModel):
    """Sanitized upstream activity label without raw evidence or source text.

    The activity recognition module owns the public activity taxonomy. This
    handoff accepts its label as a bounded string and does not redefine that
    taxonomy inside the personality package.
    """

    model_config = ConfigDict(extra="forbid")

    activity: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0.0, le=1.0)
    classifier: PetClassificationSource


class PetDecisionContext(BaseModel):
    """Minimal non-content context used to keep personality wording varied."""

    model_config = ConfigDict(extra="forbid")

    recent_messages: list[RecentMessage] = Field(default_factory=list, max_length=20)


class PetDecisionInput(BaseModel):
    """Public input boundary for one personality decision."""

    model_config = ConfigDict(extra="forbid")

    signal: RedactedActivitySignal
    classification: PetClassificationHint | None = None
    context: PetDecisionContext = Field(default_factory=PetDecisionContext)


PetDecisionOutput: TypeAlias = PersonalityResponseProposal | None
