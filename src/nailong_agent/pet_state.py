from __future__ import annotations

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
