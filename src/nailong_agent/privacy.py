"""Privacy boundary for desktop activity collection and remote inference.

Collectors must call :class:`PrivacyPolicy` before publishing, persisting, or
sending an activity event anywhere.  Raw window titles and arbitrary collector
metadata never leave this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nailong_agent.events import ActivityEvent, ActivityType, RawActivitySignal
from refactor_agent.artifacts import sanitize_text


ConsentScope = Literal["activity_collection", "remote_inference"]

_SENSITIVE_MARKERS = (
    "password",
    "passcode",
    "credential",
    "token",
    "secret",
    "api key",
    "apikey",
    "authorization",
    "auth",
    "ssh",
    "id_rsa",
    "id_ed25519",
    "authorized_keys",
    ".env",
    ".npmrc",
    "密码",
    "口令",
    "令牌",
    "密钥",
    "凭据",
    "认证",
)
_MEETING_MARKERS = ("teams", "zoom", "meet", "webex", "lark", "飞书", "腾讯会议", "钉钉会议")
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|passcode|token|secret|api[_ -]?key)\s*[:=]\s*[^\s,;]+")
_PATH = re.compile(r"(?:(?:[A-Za-z]:)?[\\/][^\s]+)+")
_APPLICATION_CATEGORIES = {
    "code": "code",
    "visual studio code": "code",
    "vscode": "code",
    "chrome": "browser",
    "firefox": "browser",
    "msedge": "browser",
    "safari": "browser",
    "cmd": "terminal",
    "powershell": "terminal",
    "pwsh": "terminal",
    "terminal": "terminal",
    "windows terminal": "terminal",
    "idea64": "ide",
    "pycharm64": "ide",
    "explorer": "explorer",
}


@dataclass(frozen=True)
class PrivacyConsent:
    """Explicit permissions; absent consent means every capability is off."""

    activity_collection_enabled: bool = False
    remote_inference_enabled: bool = False
    decision_recorded: bool = True

    @classmethod
    def unanswered(cls) -> "PrivacyConsent":
        return cls(decision_recorded=False)

    def permits(self, scope: ConsentScope) -> bool:
        return self.activity_collection_enabled if scope == "activity_collection" else self.remote_inference_enabled


@dataclass(frozen=True)
class CollectionDecision:
    allowed: bool
    reason: str
    event: ActivityEvent | None = None


class PrivacyPolicy:
    """Fail-closed filtering and minimisation for desktop activity signals."""

    def __init__(self, consent: PrivacyConsent | None = None) -> None:
        self.consent = consent or PrivacyConsent.unanswered()

    @property
    def needs_initial_consent(self) -> bool:
        return not self.consent.decision_recorded

    def admit_activity(self, event: RawActivitySignal) -> CollectionDecision:
        if not self.consent.permits("activity_collection"):
            return CollectionDecision(False, "activity_collection_not_authorized")
        if event.sensitivity != "public":
            return CollectionDecision(False, f"event_marked_{event.sensitivity}")
        if self._is_meeting(event):
            return CollectionDecision(False, "meeting_window")
        if self._contains_sensitive_marker(event):
            return CollectionDecision(False, "sensitive_window_or_content")
        return CollectionDecision(True, "minimized_local_event", self._minimize(event))

    def prepare_remote_summary(self, event: ActivityEvent) -> str | None:
        """Return the only activity representation allowed to reach DeepSeek.

        The remote path deliberately accepts a previously admitted event rather
        than arbitrary prompt text, so source code, screenshots, clipboard data,
        and raw window titles have no route into the request.
        """

        if not self.consent.permits("remote_inference"):
            return None
        if event.sensitivity != "public":
            return None
        summary = event.summary or (
            f"application={event.application_id}; activity={event.activity.value}; source={event.source}"
        )
        return self.redact_text_for_remote(summary)

    @staticmethod
    def redact_text_for_remote(value: str) -> str:
        """Defence in depth for fixed, non-code prompt fragments only."""

        value = sanitize_text(value)
        value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
        return _PATH.sub("[PATH]", value)

    @staticmethod
    def _minimize(event: RawActivitySignal) -> ActivityEvent:
        application = _normalize_application_id(event.application_id)
        activity, confidence = _classify(event)
        summary = f"application={application}; activity={activity.value}; source={event.source}"
        return ActivityEvent(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            source=event.source,
            application_id=application,
            activity=activity,
            confidence=confidence,
            summary=summary,
            sensitivity="public",
        )

    @staticmethod
    def _is_meeting(event: RawActivitySignal) -> bool:
        text = " ".join(_event_values(event))
        return bool(event.metadata.get("is_meeting_likely")) or any(marker in text for marker in _MEETING_MARKERS)

    @staticmethod
    def _contains_sensitive_marker(event: RawActivitySignal) -> bool:
        text = " ".join(_event_values(event))
        return any(marker in text for marker in _SENSITIVE_MARKERS)


def _event_values(event: RawActivitySignal) -> list[str]:
    values = [event.application_id, event.window_title_summary or "", event.activity_hint or ""]
    values.extend(str(key) for key in event.metadata)
    values.extend(str(value) for value in event.metadata.values() if value is not None)
    return [value.casefold() for value in values]


def _normalize_application_id(value: str) -> str:
    name = re.split(r"[\\/]", value.strip())[-1].casefold()
    executable = name.removesuffix(".exe")
    return _APPLICATION_CATEGORIES.get(executable, "other")


def _classify(event: RawActivitySignal) -> tuple[ActivityType, float]:
    if event.activity != ActivityType.UNKNOWN:
        return event.activity, event.confidence
    if event.source == "idle":
        return ActivityType.IDLE, max(event.confidence, 1.0)
    hint = (event.activity_hint or "").casefold()
    rules = (
        (ActivityType.DEBUGGING, ("debug", "traceback", "调试")),
        (ActivityType.CODING, ("edit", "coding", "code", "编程", "编码")),
        (ActivityType.READING, ("read", "阅读")),
        (ActivityType.WRITING, ("write", "文档", "写作")),
        (ActivityType.GAMING, ("game", "gaming", "游戏")),
        (ActivityType.MEDIA, ("video", "media", "music", "视频", "音乐")),
    )
    for activity, markers in rules:
        if any(marker in hint for marker in markers):
            return activity, max(event.confidence, 0.7)
    return ActivityType.UNKNOWN, event.confidence
