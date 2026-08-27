from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nailong_agent.events import PetExpression, PetState, PopupDecision
from nailong_agent.health import NailongHealthSnapshot
from nailong_agent.pet_state import PetEmotion, PetPersonalityState
from nailong_agent.privacy import PrivacyConsent


@dataclass(frozen=True)
class BubblePlacement:
    x: int
    y: int
    tail_x: int


def clamp_window_position(
    *,
    available: tuple[int, int, int, int],
    window_size: tuple[int, int],
    desired: tuple[int, int],
) -> tuple[int, int]:
    """Keep a dragged window fully inside one screen's usable geometry."""

    available_x, available_y, available_width, available_height = available
    window_width, window_height = window_size
    desired_x, desired_y = desired
    maximum_x = max(available_x, available_x + available_width - window_width)
    maximum_y = max(available_y, available_y + available_height - window_height)
    return (
        min(max(desired_x, available_x), maximum_x),
        min(max(desired_y, available_y), maximum_y),
    )


def place_bubble_above_pet(
    *,
    available: tuple[int, int, int, int],
    pet: tuple[int, int, int, int],
    bubble_size: tuple[int, int],
    screen_margin: int = 12,
    pet_gap: int = 6,
    tail_margin: int = 28,
) -> BubblePlacement:
    """Place a bubble above the pet and clamp it to the usable screen."""

    available_x, available_y, available_width, _ = available
    pet_x, pet_y, pet_width, _ = pet
    bubble_width, bubble_height = bubble_size
    minimum_x = available_x + screen_margin
    maximum_x = max(minimum_x, available_x + available_width - screen_margin - bubble_width)
    pet_center_x = pet_x + pet_width // 2
    x = min(max(pet_center_x - bubble_width // 2, minimum_x), maximum_x)
    minimum_y = available_y + screen_margin
    y = max(minimum_y, pet_y - bubble_height - pet_gap)
    tail_x = min(max(pet_center_x - x, tail_margin), max(tail_margin, bubble_width - tail_margin))
    return BubblePlacement(x=x, y=y, tail_x=tail_x)


MOUTH_TEXT = {
    PetExpression.NEUTRAL: "（嘴巴）",
    PetExpression.HAPPY: "（微笑）",
    PetExpression.LAUGH: "（大笑）",
    PetExpression.CONCERNED: "（担忧）",
    PetExpression.SLEEPY: "（困倦）",
}


def decision_to_pet_state(decision: PopupDecision) -> PetState:
    reason = decision.reason.casefold()
    message = (decision.message or "").casefold()
    context = f"{reason} {message}"
    if any(word in context for word in ("success", "complete", "passed", "完成", "通过", "成功")):
        expression = PetExpression.LAUGH
    elif any(word in context for word in ("fail", "error", "broken", "失败", "错误")):
        expression = PetExpression.CONCERNED
    elif any(word in context for word in ("sleep", "idle", "defer", "待机", "困")):
        expression = PetExpression.SLEEPY
    elif decision.action == "show":
        expression = PetExpression.HAPPY
    else:
        expression = PetExpression.NEUTRAL
    return PetState(
        expression=expression,
        bubble_text=decision.message,
        bubble_visible=decision.action == "show" and bool(decision.message),
        bubble_seconds=decision.display_seconds,
    )


def personality_state_to_pet_state(state: PetPersonalityState) -> PetState:
    expressions = {
        PetEmotion.CHEERFUL: PetExpression.HAPPY,
        PetEmotion.CURIOUS: PetExpression.HAPPY,
        PetEmotion.CONCERNED: PetExpression.CONCERNED,
        PetEmotion.SLEEPY: PetExpression.SLEEPY,
        PetEmotion.CELEBRATING: PetExpression.LAUGH,
        PetEmotion.NEUTRAL: PetExpression.NEUTRAL,
    }
    return PetState(
        expression=expressions[state.emotion],
        bubble_visible=False,
        bubble_seconds=0,
    )


class PopupRenderer(Protocol):
    def start(self) -> None: ...

    def show(self, decision: PopupDecision) -> bool | None: ...

    def stop(self) -> None: ...

    def exec(self) -> int: ...


PopupDeliveryResult = Callable[[str, str], None]


class PrivacyControlsRenderer(Protocol):
    """Optional renderer extension; legacy renderers remain compatible."""

    def request_privacy_consent(self) -> PrivacyConsent | None: ...

    def configure_privacy_controls(
        self,
        *,
        on_clear_activity_history: Callable[[], int],
        get_privacy_consent: Callable[[], PrivacyConsent] | None = None,
        on_save_privacy_consent: Callable[[PrivacyConsent], None] | None = None,
    ) -> None: ...


class NotificationControlsRenderer(Protocol):
    """Optional all-day do-not-disturb control exposed by desktop renderers."""

    def configure_notification_controls(
        self,
        *,
        on_set_do_not_disturb: Callable[[bool], None],
        get_do_not_disturb: Callable[[], bool],
        on_set_manual_pause: Callable[[bool], None] | None = None,
        get_manual_pause: Callable[[], bool] | None = None,
        on_set_game_tease: Callable[[bool], None] | None = None,
        get_game_tease: Callable[[], bool] | None = None,
        on_set_python_review: Callable[[bool], None] | None = None,
        get_python_review: Callable[[], bool] | None = None,
    ) -> None: ...


class HealthControlsRenderer(Protocol):
    def configure_health_controls(
        self,
        *,
        get_health_snapshot: Callable[[], NailongHealthSnapshot],
    ) -> None: ...


class NullRenderer:
    """Headless renderer used by tests and command-line smoke runs."""

    def __init__(self) -> None:
        self.decisions: list[PopupDecision] = []
        self.states: list[PetState] = []
        self.started = False
        self.consent_response: PrivacyConsent | None = None
        self.consent_requested = False
        self._on_clear_activity_history: Callable[[], int] | None = None
        self._get_privacy_consent: Callable[[], PrivacyConsent] | None = None
        self._on_save_privacy_consent: Callable[[PrivacyConsent], None] | None = None
        self._on_set_do_not_disturb: Callable[[bool], None] | None = None
        self._on_set_manual_pause: Callable[[bool], None] | None = None
        self._get_manual_pause: Callable[[], bool] | None = None
        self._get_do_not_disturb: Callable[[], bool] | None = None
        self._on_set_game_tease: Callable[[bool], None] | None = None
        self._get_game_tease: Callable[[], bool] | None = None
        self._on_set_python_review: Callable[[bool], None] | None = None
        self._get_python_review: Callable[[], bool] | None = None
        self._get_health_snapshot: Callable[[], NailongHealthSnapshot] | None = None
        self._on_popup_delivery: PopupDeliveryResult | None = None

    def start(self) -> None:
        self.started = True

    def show(self, decision: PopupDecision) -> bool:
        state = decision_to_pet_state(decision)
        self.states.append(state)
        if decision.action == "show":
            self.decisions.append(decision)
            if decision.dedupe_key and self._on_popup_delivery is not None:
                self._on_popup_delivery(decision.dedupe_key, "shown")
            return True
        return False

    def can_present_popup(self) -> bool:
        return True

    def configure_popup_delivery(self, on_result: PopupDeliveryResult) -> None:
        self._on_popup_delivery = on_result

    def apply_personality_state(self, state: PetPersonalityState) -> None:
        self.states.append(personality_state_to_pet_state(state))

    def stop(self) -> None:
        self.started = False

    def exec(self) -> int:
        return 0

    def request_privacy_consent(self) -> PrivacyConsent | None:
        self.consent_requested = True
        return self.consent_response

    def configure_privacy_controls(
        self,
        *,
        on_clear_activity_history: Callable[[], int],
        get_privacy_consent: Callable[[], PrivacyConsent] | None = None,
        on_save_privacy_consent: Callable[[PrivacyConsent], None] | None = None,
    ) -> None:
        self._on_clear_activity_history = on_clear_activity_history
        self._get_privacy_consent = get_privacy_consent
        self._on_save_privacy_consent = on_save_privacy_consent

    def save_privacy_consent(self, consent: PrivacyConsent) -> None:
        if self._on_save_privacy_consent is None:
            raise RuntimeError("privacy consent controls are not configured")
        self._on_save_privacy_consent(consent)

    def configure_notification_controls(
        self,
        *,
        on_set_do_not_disturb: Callable[[bool], None],
        get_do_not_disturb: Callable[[], bool],
        on_set_manual_pause: Callable[[bool], None] | None = None,
        get_manual_pause: Callable[[], bool] | None = None,
        on_set_game_tease: Callable[[bool], None] | None = None,
        get_game_tease: Callable[[], bool] | None = None,
        on_set_python_review: Callable[[bool], None] | None = None,
        get_python_review: Callable[[], bool] | None = None,
    ) -> None:
        self._on_set_do_not_disturb = on_set_do_not_disturb
        self._on_set_manual_pause = on_set_manual_pause
        self._get_manual_pause = get_manual_pause
        self._get_do_not_disturb = get_do_not_disturb
        self._on_set_game_tease = on_set_game_tease
        self._get_game_tease = get_game_tease
        self._on_set_python_review = on_set_python_review
        self._get_python_review = get_python_review

    def set_do_not_disturb(self, enabled: bool) -> None:
        if self._on_set_do_not_disturb is None:
            raise RuntimeError("notification controls are not configured")
        self._on_set_do_not_disturb(enabled)

    def set_manual_pause(self, enabled: bool) -> None:
        if self._on_set_manual_pause is None:
            raise RuntimeError("manual pause controls are not configured")
        self._on_set_manual_pause(enabled)

    def set_game_tease(self, enabled: bool) -> None:
        if self._on_set_game_tease is None:
            raise RuntimeError("game tease controls are not configured")
        self._on_set_game_tease(enabled)

    def set_python_review(self, enabled: bool) -> None:
        if self._on_set_python_review is None:
            raise RuntimeError("python review controls are not configured")
        self._on_set_python_review(enabled)

    def configure_health_controls(
        self,
        *,
        get_health_snapshot: Callable[[], NailongHealthSnapshot],
    ) -> None:
        self._get_health_snapshot = get_health_snapshot

    def get_health_snapshot(self) -> NailongHealthSnapshot:
        if self._get_health_snapshot is None:
            raise RuntimeError("health controls are not configured")
        return self._get_health_snapshot()
