from datetime import datetime, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PetPersonalityResponse,
    PersonalityScenario,
    RedactedActivitySignal,
)



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






def test_pet_decision_output_is_content_only() -> None:
    assert set(get_args(PetDecisionOutput)) == {PetPersonalityResponse, type(None)}
    output = PetPersonalityResponse(
        persona_version="nailong-v1.1-standard",
        message="看吧，还得是本龙。",
        intent="celebrate",
    )

    assert output.intent == "celebrate"
    assert set(output.model_dump()) == {"persona_version", "message", "intent"}
