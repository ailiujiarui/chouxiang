from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypedDict

from nailong_agent.contracts import (
    PetClassificationHint,
    PetClassificationSource,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PetSituation,
    RedactedActivitySignal,
)
from nailong_agent.events import PersonalityResponseProposal


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


PolicyAction = Literal["show", "defer", "drop"]


@dataclass(frozen=True)
class InterruptionPolicy:
    """User-respecting defaults for proactive desktop-pet messages."""

    daily_popup_limit: int = 8
    low_priority_cooldown_seconds: int = 30 * 60
    normal_priority_cooldown_seconds: int = 15 * 60
    high_priority_cooldown_seconds: int = 5 * 60

    def __post_init__(self) -> None:
        values = (
            self.daily_popup_limit,
            self.low_priority_cooldown_seconds,
            self.normal_priority_cooldown_seconds,
            self.high_priority_cooldown_seconds,
        )
        if any(value < 0 for value in values):
            raise ValueError("interruption policy limits must be non-negative")

    def cooldown_seconds(self, priority: str) -> int:
        return {
            "low": self.low_priority_cooldown_seconds,
            "normal": self.normal_priority_cooldown_seconds,
            "high": self.high_priority_cooldown_seconds,
        }[priority]


class PetGraphState(TypedDict, total=False):
    """State owned only by the desktop personality decision graph."""

    decision_input: PetDecisionInput
    signal: RedactedActivitySignal
    context: PetDecisionContext
    provided_classification: PetClassificationHint | None
    situation: PetSituation
    classification_confidence: float
    classification_source: PetClassificationSource
    emotion: PetEmotion
    response: PersonalityResponseProposal
    policy_action: PolicyAction
    policy_reason: str
    decision: PetDecisionOutput
    llm_used: bool
    llm_error: str | None
    node_trace: list[str]
