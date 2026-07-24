from __future__ import annotations

from enum import StrEnum
from typing import Literal, TypedDict

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
