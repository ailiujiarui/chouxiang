from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PetSituation,
    RedactedActivitySignal,
)
from nailong_agent.events import PopupDecision


def test_pet_decision_input_accepts_only_redacted_activity_data() -> None:
    decision_input = PetDecisionInput(
        signal=RedactedActivitySignal(
            source="ide",
            application_id="vscode",
            activity_hint="test-result",
            confidence=0.98,
            redacted_summary="pytest reported a failure",
        ),
        classification=PetClassificationHint(
            situation=PetSituation.TEST_FAILED,
            confidence=0.98,
            classifier="rules",
        ),
    )

    assert decision_input.signal.application_id == "vscode"
    assert decision_input.classification is not None
    assert decision_input.classification.situation == PetSituation.TEST_FAILED
    assert decision_input.context.paused is False


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
        RedactedActivitySignal(
            source="ide",
            application_id="vscode",
            confidence=1.1,
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


def test_decision_context_contains_only_non_content_policy_state() -> None:
    context = PetDecisionContext(
        now=datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        paused=True,
        quiet_hours_active=True,
        is_fullscreen=True,
        is_meeting=True,
        daily_popup_count=3,
        last_popup_at=datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc),
        recent_messages=["哼，本龙听见了。"],
    )

    assert context.paused is True
    assert context.daily_popup_count == 3
    assert context.recent_messages == ["哼，本龙听见了。"]


def test_pet_situation_covers_required_personality_scenarios() -> None:
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
    } == {item.value for item in PetSituation}


def test_pet_decision_output_reuses_popup_decision_contract() -> None:
    assert PetDecisionOutput is PopupDecision
    output = PetDecisionOutput(
        action="show",
        reason="confirmed test success",
        message="看吧，还得是本龙。",
    )

    assert output.action == "show"
