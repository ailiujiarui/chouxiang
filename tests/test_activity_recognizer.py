from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nailong_agent.activity_recognizer import ActivityRecognizer
from nailong_agent.events import ActivityType, ActivityWindow
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy


class FakeProvider:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
            }
        )
        return self.result


def test_explicit_activity_rule_skips_local_and_remote_classifiers() -> None:
    provider = FakeProvider({"activity": "reading", "confidence": 0.9})
    recognizer = ActivityRecognizer(
        provider=provider,
        privacy_policy=PrivacyPolicy(PrivacyConsent(remote_inference_enabled=True)),
    )

    result = recognizer.classify(_window(activity=ActivityType.DEBUGGING, confidence=0.95))

    assert result.activity is ActivityType.DEBUGGING
    assert result.confidence == 0.95
    assert result.classifier == "rules"
    assert provider.calls == []


def test_lightweight_classifier_handles_ambiguous_browser_window_locally() -> None:
    recognizer = ActivityRecognizer()

    result = recognizer.classify(_window(application="browser"))

    assert result.activity is ActivityType.READING
    assert result.confidence >= 0.65
    assert result.classifier == "lightweight"


def test_remote_classifier_only_handles_low_confidence_authorized_window() -> None:
    provider = FakeProvider({"activity": "debugging", "confidence": 0.88})
    recognizer = ActivityRecognizer(
        provider=provider,
        privacy_policy=PrivacyPolicy(PrivacyConsent(remote_inference_enabled=True)),
    )

    result = recognizer.classify(
        _window(application="other", summary="token=secret-value", confidence=0.1)
    )

    assert result.activity is ActivityType.DEBUGGING
    assert result.confidence == 0.88
    assert result.classifier == "llm"
    assert len(provider.calls) == 1
    assert "secret-value" not in str(provider.calls[0]["user_prompt"])
    assert "untrusted data" in str(provider.calls[0]["system_prompt"])


def test_low_confidence_window_never_calls_remote_without_consent() -> None:
    provider = FakeProvider({"activity": "debugging", "confidence": 0.88})
    recognizer = ActivityRecognizer(
        provider=provider,
        privacy_policy=PrivacyPolicy(PrivacyConsent(remote_inference_enabled=False)),
    )

    result = recognizer.classify(_window(application="other", confidence=0.1))

    assert result.activity is ActivityType.UNKNOWN
    assert result.classifier == "lightweight"
    assert provider.calls == []


def _window(
    *,
    application: str = "code",
    activity: ActivityType = ActivityType.UNKNOWN,
    confidence: float = 0.2,
    summary: str | None = "application=code; activity=unknown; source=window",
) -> ActivityWindow:
    started_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    return ActivityWindow(
        window_started_at=started_at,
        window_ended_at=started_at + timedelta(seconds=60),
        dominant_application=application,
        dominant_activity=activity,
        confidence=confidence,
        event_count=1,
        summary=summary,
    )
