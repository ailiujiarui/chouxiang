from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from nailong_agent.events import ActivityEvent, ActivityType, PetPreferences
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.privacy import PrivacyConsent
from nailong_agent.python_review import (
    AutomaticPythonReviewService,
    DeepSeekPythonReviewer,
    PythonReviewBubbleFormatter,
    PythonReviewResult,
    PythonSourceSnapshot,
    RecentPythonSourceProvider,
)


NOW = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)


class FakeSourceProvider:
    def __init__(self, snapshot: PythonSourceSnapshot | None) -> None:
        self.snapshot = snapshot
        self.primed = False

    def prime(self) -> None:
        self.primed = True

    def capture_latest(self) -> PythonSourceSnapshot | None:
        snapshot, self.snapshot = self.snapshot, None
        return snapshot


class FakeReviewer:
    def __init__(self) -> None:
        self.snapshots: list[PythonSourceSnapshot] = []

    def review(self, snapshot: PythonSourceSnapshot) -> PythonReviewResult:
        self.snapshots.append(snapshot)
        return PythonReviewResult(
            status="completed",
            severity="medium",
            findings=["异常分支缺少测试", "函数职责偏多"],
            suggestion="先补一条失败路径测试",
            confidence=0.88,
        )


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "severity": "low",
            "findings": ["边界处理清楚"],
            "suggestion": "补充空输入测试",
            "confidence": 0.91,
        }


def _snapshot(source: str = "def value():\n    return 1\n") -> PythonSourceSnapshot:
    import hashlib

    return PythonSourceSnapshot(
        filename="example.py",
        source=source,
        fingerprint=hashlib.sha256(source.encode()).hexdigest(),
    )


def _coding_event() -> ActivityEvent:
    return ActivityEvent(
        event_id="coding-1",
        occurred_at=NOW,
        source="window",
        application_id="code",
        activity=ActivityType.CODING,
        confidence=0.7,
        summary="application=code; activity=coding; source=window",
    )


def _service(tmp_path: Path, *, consent: PrivacyConsent):
    store = NotificationStore(tmp_path / "notifications.sqlite")
    notifications = NotificationService(store=store, clock=lambda: NOW)
    source = FakeSourceProvider(_snapshot())
    reviewer = FakeReviewer()
    service = AutomaticPythonReviewService(
        source_provider=source,
        reviewer=reviewer,
        formatter=PythonReviewBubbleFormatter(),
        notifications=notifications,
        store=store,
        consent=lambda: consent,
        preferences=lambda: PetPreferences(),
        clock=lambda: NOW,
        cooldown_seconds=600,
    )
    service.on_activity_event(_coding_event().envelope())
    return service, source, reviewer, notifications


def test_recent_source_provider_only_reads_files_saved_after_prime(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("def before():\n    return 1\n", encoding="utf-8")
    provider = RecentPythonSourceProvider([tmp_path])
    provider.prime()

    assert provider.capture_latest() is None

    target.write_text("def after():\n    return 2\n", encoding="utf-8")
    snapshot = provider.capture_latest()

    assert snapshot is not None
    assert snapshot.filename == "sample.py"
    assert "def after" in snapshot.source


def test_recent_source_provider_rejects_literal_secrets(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    provider = RecentPythonSourceProvider([tmp_path])
    provider.prime()
    target.write_text('api_key = "not-a-real-secret"\n', encoding="utf-8")

    assert provider.capture_latest() is None


def test_automatic_review_requires_explicit_source_upload_consent(tmp_path: Path) -> None:
    service, _, reviewer, notifications = _service(
        tmp_path,
        consent=PrivacyConsent(activity_collection_enabled=True),
    )

    assert service.process_once() is False
    assert reviewer.snapshots == []
    assert notifications.get_status().pending_count == 0


def test_automatic_review_sends_tsundere_report_to_notification_pipeline(tmp_path: Path) -> None:
    service, _, reviewer, notifications = _service(
        tmp_path,
        consent=PrivacyConsent(
            activity_collection_enabled=True,
            python_review_enabled=True,
        ),
    )

    assert service.process_once() is True
    assert len(reviewer.snapshots) == 1
    assert notifications.get_status().pending_count == 1
    intent = notifications.lease_next()
    assert intent is not None
    assert "本龙" in intent.message
    assert "异常分支缺少测试" in intent.message


def test_python_review_store_enforces_content_dedupe_and_cooldown(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "notifications.sqlite")
    fingerprint = _snapshot().fingerprint

    first = store.reserve_python_review(
        fingerprint=fingerprint,
        now=NOW,
        cooldown_seconds=600,
        maximum_reviews_per_day=3,
    )
    second = store.reserve_python_review(
        fingerprint=fingerprint,
        now=NOW,
        cooldown_seconds=600,
        maximum_reviews_per_day=3,
    )

    assert first is not None
    assert second is None


def test_deepseek_reviewer_uses_static_metrics_and_untrusted_source_boundary() -> None:
    provider = FakeProvider()
    result = DeepSeekPythonReviewer(lambda: provider).review(_snapshot())

    assert result.status == "completed"
    assert result.severity == "low"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert "untrusted data" in str(call["system_prompt"])
    payload = json.loads(str(call["user_prompt"]))
    assert payload["static_metrics"]["loc"] == 2
    assert payload["source"].startswith("def value")


def test_failed_review_bubble_does_not_claim_success() -> None:
    response = PythonReviewBubbleFormatter().format(
        PythonReviewResult(status="failed", error_code="llm_unavailable")
    )

    assert "没跑完" in response.message
    assert response.intent == "remind"
