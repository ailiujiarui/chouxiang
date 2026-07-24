from datetime import datetime, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PersonalityScenario,
    RedactedActivitySignal,
)
from nailong_agent.events import PersonalityResponseProposal


def test_pet_decision_input_accepts_only_redacted_activity_data() -> None:
    decision_input = PetDecisionInput(
        signal=RedactedActivitySignal(
            source="ide",
            application_id="vscode",
            redacted_summary="pytest reported a failure",
        ),
        classification=PetClassificationHint(
            activity="test_failed",
            confidence=0.98,
            classifier="rules",
        ),
    )

    assert decision_input.signal.application_id == "vscode"
    assert decision_input.classification is not None
    assert decision_input.classification.activity == "test_failed"
    assert decision_input.context.recent_messages == []


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "raw_code",
        "clipboard",
        "screenshot",
        "terminal_text",
        "window_title",
        "credentials",
        "metadata",
        "activity_hint",
        "confidence",
    ],
)
def test_redacted_signal_rejects_forbidden_extra_fields(forbidden_field: str) -> None:
    payload = {
        "source": "ide",
        "application_id": "vscode",
        forbidden_field: "untrusted secret content",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RedactedActivitySignal.model_validate(payload)


def test_redacted_signal_enforces_bounds_and_aware_time() -> None:
    with pytest.raises(ValidationError):
        PetClassificationHint(
            activity="test_failed",
            confidence=1.1,
            classifier="rules",
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        RedactedActivitySignal(
            source="ide",
            application_id="vscode",
            occurred_at=datetime(2026, 7, 24, 9, 0),
        )
    with pytest.raises(ValidationError):
        RedactedActivitySignal(
            source="ide",
            application_id="vscode",
            redacted_summary="x" * 501,
        )


def test_decision_context_contains_only_recent_personality_messages() -> None:
    context = PetDecisionContext(
        recent_messages=["哼，本龙听见了。"],
    )

    assert context.recent_messages == ["哼，本龙听见了。"]


def test_upstream_activity_label_is_not_restricted_by_personality_contract() -> None:
    hint = PetClassificationHint(
        activity="future_team_defined_activity",
        confidence=0.8,
        classifier="future_team_defined_classifier",
    )

    assert hint.activity == "future_team_defined_activity"
    assert hint.classifier == "future_team_defined_classifier"


def test_internal_personality_scenario_covers_required_responses() -> None:
    assert {
        "coding",
        "debugging",
        "test_failed",
        "test_succeeded",
        "compile_succeeded",
        "long_work",
        "idle",
        "meeting",
        "entertainment",
        "unknown",
    } == {item.value for item in PersonalityScenario}


def test_pet_decision_output_is_an_optional_personality_proposal() -> None:
    assert set(get_args(PetDecisionOutput)) == {PersonalityResponseProposal, type(None)}
    output = PersonalityResponseProposal(
        persona_version="nailong-v1.1-standard",
        emotion="celebrating",
        message="看吧，还得是本龙。",
        intent="celebrate",
    )

    assert output.intent == "celebrate"
