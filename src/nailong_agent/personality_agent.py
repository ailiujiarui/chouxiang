from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nailong_agent.contracts import (
    PetClassificationSource,
    PetDecisionInput,
    PetDecisionOutput,
    PetSituation,
)
from nailong_agent.events import PersonalityResponseProposal, PopupDecision
from nailong_agent.pet_graph import run_pet_graph
from nailong_agent.pet_prompts import (
    PET_CLASSIFICATION_SYSTEM_PROMPT,
    build_pet_classification_user_prompt,
)
from nailong_agent.pet_state import (
    InterruptionPolicy,
    PersonalityIntensity,
    PetEmotion,
    PetGraphState,
)
from refactor_agent.llm import LLMProvider


class _LLMClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    situation: PetSituation
    confidence: float = Field(ge=0.0, le=1.0)


_EMOTIONS = {
    PetSituation.CODING: PetEmotion.CURIOUS,
    PetSituation.DEBUGGING: PetEmotion.CONCERNED,
    PetSituation.TEST_FAILED: PetEmotion.CONCERNED,
    PetSituation.TEST_SUCCEEDED: PetEmotion.CELEBRATING,
    PetSituation.COMPILE_SUCCEEDED: PetEmotion.CHEERFUL,
    PetSituation.LONG_WORK: PetEmotion.SLEEPY,
    PetSituation.IDLE: PetEmotion.SLEEPY,
    PetSituation.MEETING: PetEmotion.NEUTRAL,
    PetSituation.ENTERTAINMENT: PetEmotion.CHEERFUL,
    PetSituation.UNKNOWN: PetEmotion.NEUTRAL,
}

_RULE_SITUATIONS = {
    "coding": PetSituation.CODING,
    "editing_code": PetSituation.CODING,
    "debug": PetSituation.DEBUGGING,
    "debugging": PetSituation.DEBUGGING,
    "debug_session": PetSituation.DEBUGGING,
    "test_failed": PetSituation.TEST_FAILED,
    "tests_failed": PetSituation.TEST_FAILED,
    "pytest_failed": PetSituation.TEST_FAILED,
    "test_succeeded": PetSituation.TEST_SUCCEEDED,
    "tests_succeeded": PetSituation.TEST_SUCCEEDED,
    "tests_passed": PetSituation.TEST_SUCCEEDED,
    "pytest_passed": PetSituation.TEST_SUCCEEDED,
    "compile_succeeded": PetSituation.COMPILE_SUCCEEDED,
    "build_succeeded": PetSituation.COMPILE_SUCCEEDED,
    "long_work": PetSituation.LONG_WORK,
    "idle": PetSituation.IDLE,
    "meeting": PetSituation.MEETING,
    "entertainment": PetSituation.ENTERTAINMENT,
    "gaming": PetSituation.ENTERTAINMENT,
    "media": PetSituation.ENTERTAINMENT,
}

_MESSAGES: dict[PersonalityIntensity, dict[PetSituation, str]] = {
    PersonalityIntensity.LOW: {
        PetSituation.DEBUGGING: "看起来还在调试。先看最近一次变化，本龙陪你理一理。",
        PetSituation.TEST_FAILED: "测试没有通过。先看第一条失败，本龙陪你一起排查。",
        PetSituation.TEST_SUCCEEDED: "测试通过了。本龙也替你高兴。",
        PetSituation.COMPILE_SUCCEEDED: "编译通过了，记得继续确认测试结果。",
        PetSituation.LONG_WORK: "你已经忙一阵了。本龙陪你休息一下再继续。",
    },
    PersonalityIntensity.STANDARD: {
        PetSituation.DEBUGGING: "哼，这个问题还挺会躲。本龙只是顺手陪你从最近一次变化开始看。",
        PetSituation.TEST_FAILED: "哼，这个测试又闹脾气了。本龙还没认输，先看第一条失败。",
        PetSituation.TEST_SUCCEEDED: "看吧，还得是本龙……和你也有那么一点功劳。",
        PetSituation.COMPILE_SUCCEEDED: "编译通过啦，勉强有本龙几分风范。下一步再确认测试。",
        PetSituation.LONG_WORK: "你已经忙很久了。本龙才不是担心你，起来喝口水再继续？",
    },
    PersonalityIntensity.HIGH: {
        PetSituation.DEBUGGING: "哼，这个问题躲得倒挺快，可躲不过本龙的龙角！先从最近一次变化查起。",
        PetSituation.TEST_FAILED: "哼，这个测试还敢闹脾气？本龙可没认输，先揪住第一条失败。",
        PetSituation.TEST_SUCCEEDED: "看吧，还得是本龙……咳，你也确实干得漂亮！",
        PetSituation.COMPILE_SUCCEEDED: "编译通过啦！勉强追上本龙甩尾巴的速度，下一步再确认测试。",
        PetSituation.LONG_WORK: "忙这么久，连本龙的零食都要放凉了。本龙才不是担心你，先喝口水！",
    },
}

_CATCHPHRASE_FREE_MESSAGES = {
    PetSituation.DEBUGGING: "这个问题躲得挺快，龙角都快被它绕晕了。先查最近一次变化。",
    PetSituation.TEST_FAILED: "这个测试又把尾巴翘起来了。先抓第一条失败，后面的噪声等等。",
    PetSituation.TEST_SUCCEEDED: "测试确实通过了，小爪子都忍不住要鼓掌。",
    PetSituation.COMPILE_SUCCEEDED: "编译已经通过，龙角接收到好消息了。下一步再确认测试。",
    PetSituation.LONG_WORK: "忙了这么久，连零食都该歇一会儿。先喝口水再继续？",
}

_CATCHPHRASE_STEMS = (
    "哼，本龙听见了",
    "这点小事，也想难住本龙",
    "本龙还没认输",
    "勉强有本龙几分风范",
    "本龙只是顺手",
    "本龙才不是担心你",
    "本龙一直都在",
    "看吧，还得是本龙",
)

_INTENTS: dict[PetSituation, Literal["encourage", "remind", "celebrate", "ask", "stay_silent"]] = {
    PetSituation.CODING: "stay_silent",
    PetSituation.DEBUGGING: "encourage",
    PetSituation.TEST_FAILED: "remind",
    PetSituation.TEST_SUCCEEDED: "celebrate",
    PetSituation.COMPILE_SUCCEEDED: "celebrate",
    PetSituation.LONG_WORK: "remind",
    PetSituation.IDLE: "stay_silent",
    PetSituation.MEETING: "stay_silent",
    PetSituation.ENTERTAINMENT: "stay_silent",
    PetSituation.UNKNOWN: "stay_silent",
}

_PRIORITIES: dict[PetSituation, Literal["low", "normal", "high"]] = {
    PetSituation.CODING: "low",
    PetSituation.DEBUGGING: "low",
    PetSituation.TEST_FAILED: "normal",
    PetSituation.TEST_SUCCEEDED: "normal",
    PetSituation.COMPILE_SUCCEEDED: "normal",
    PetSituation.LONG_WORK: "low",
    PetSituation.IDLE: "low",
    PetSituation.MEETING: "low",
    PetSituation.ENTERTAINMENT: "low",
    PetSituation.UNKNOWN: "low",
}


class PetPersonalityAgent:
    """Deterministic personality graph with optional low-confidence LLM classification."""

    def __init__(
        self,
        *,
        provider: LLMProvider | None = None,
        intensity: PersonalityIntensity | str = PersonalityIntensity.STANDARD,
        interruption_policy: InterruptionPolicy | None = None,
        high_confidence_threshold: float = 0.75,
        response_confidence_threshold: float = 0.65,
        backend: str = "loop",
    ) -> None:
        if not 0.0 <= high_confidence_threshold <= 1.0:
            raise ValueError("high_confidence_threshold must be between 0 and 1")
        if not 0.0 <= response_confidence_threshold <= 1.0:
            raise ValueError("response_confidence_threshold must be between 0 and 1")
        self.provider = provider
        self.intensity = PersonalityIntensity(intensity)
        self.interruption_policy = interruption_policy or InterruptionPolicy()
        self.high_confidence_threshold = high_confidence_threshold
        self.response_confidence_threshold = response_confidence_threshold
        self.backend = backend

    def decide(self, decision_input: PetDecisionInput) -> PetDecisionOutput:
        return self.run(decision_input)["decision"]

    def run(self, decision_input: PetDecisionInput) -> PetGraphState:
        validated = PetDecisionInput.model_validate(decision_input)
        return run_pet_graph(
            {"decision_input": validated, "node_trace": []},
            self,
            backend=self.backend,
        )

    def observe(self, state: PetGraphState) -> PetGraphState:
        decision_input = PetDecisionInput.model_validate(state["decision_input"])
        return {
            **state,
            "decision_input": decision_input,
            "signal": decision_input.signal,
            "provided_classification": decision_input.classification,
            "context": decision_input.context,
            "llm_used": False,
            "llm_error": None,
        }

    def classify(self, state: PetGraphState) -> PetGraphState:
        signal = state["signal"]
        provided = state.get("provided_classification")
        if signal.sensitivity != "public":
            return self._classified(
                state,
                PetSituation.UNKNOWN,
                0.0,
                "collector",
            )

        rule_situation = _rule_situation(signal.activity_hint)
        if rule_situation is not PetSituation.UNKNOWN:
            return self._classified(
                state,
                rule_situation,
                max(signal.confidence, 0.9),
                "rules",
            )

        if provided is not None and provided.confidence >= self.high_confidence_threshold:
            return self._classified(
                state,
                provided.situation,
                provided.confidence,
                provided.classifier,
            )

        if self.provider is None:
            if provided is not None:
                return self._classified(
                    state,
                    provided.situation,
                    provided.confidence,
                    provided.classifier,
                )
            return self._classified(state, PetSituation.UNKNOWN, 0.0, "classifier")

        try:
            raw_result = self.provider.complete_json(
                system_prompt=PET_CLASSIFICATION_SYSTEM_PROMPT,
                user_prompt=build_pet_classification_user_prompt(signal),
                temperature=0.0,
            )
            result = _LLMClassification.model_validate(raw_result)
        except Exception as exc:
            fallback = provided.situation if provided is not None else PetSituation.UNKNOWN
            confidence = provided.confidence if provided is not None else 0.0
            source = provided.classifier if provided is not None else "classifier"
            return {
                **self._classified(state, fallback, confidence, source),
                "llm_used": True,
                "llm_error": type(exc).__name__,
            }
        return {
            **self._classified(state, result.situation, result.confidence, "llm"),
            "llm_used": True,
        }

    def infer_emotion(self, state: PetGraphState) -> PetGraphState:
        return {
            **state,
            "emotion": _EMOTIONS[state["situation"]],
        }

    def choose_personality_response(self, state: PetGraphState) -> PetGraphState:
        situation = state["situation"]
        signal = state["signal"]
        confidence = state["classification_confidence"]
        intent = _INTENTS[situation]
        message = _MESSAGES[self.intensity].get(situation, "保持安静")
        if situation in _CATCHPHRASE_FREE_MESSAGES and _should_avoid_catchphrase(
            message,
            state["context"].recent_messages,
        ):
            message = _CATCHPHRASE_FREE_MESSAGES[situation]

        if confidence < self.response_confidence_threshold:
            intent = "stay_silent"
            message = "信息不足，保持安静"
        if signal.source == "user" and intent == "stay_silent":
            intent = "ask"
            message = "哼，本龙听见了。本龙先不乱猜，你想让本龙陪你看什么？"

        response = PersonalityResponseProposal(
            persona_version=f"nailong-v1.1-{self.intensity.value}",
            emotion=state["emotion"].value,
            message=message,
            intent=intent,
            priority=_PRIORITIES[situation],
            expires_in_seconds=300,
        )
        return {**state, "response": response}

    def apply_interruption_policy(self, state: PetGraphState) -> PetGraphState:
        signal = state["signal"]
        context = state["context"]
        response = state["response"]

        if signal.sensitivity != "public":
            return self._policy_result(state, "drop", "sensitive_activity")
        if context.paused:
            return self._policy_result(state, "drop", "manually_paused")
        if context.quiet_hours_active:
            return self._policy_result(state, "drop", "quiet_hours")
        if context.is_meeting or state["situation"] is PetSituation.MEETING:
            return self._policy_result(state, "drop", "meeting")
        if response.intent == "stay_silent":
            return self._policy_result(state, "drop", "personality_chose_silence")
        if context.daily_popup_count >= self.interruption_policy.daily_popup_limit:
            return self._policy_result(state, "drop", "daily_popup_limit")
        if context.is_fullscreen:
            return self._policy_result(state, "defer", "fullscreen")
        if response.message in context.recent_messages:
            return self._policy_result(state, "drop", "duplicate_message")
        if context.last_popup_at is not None:
            elapsed = (context.now - context.last_popup_at).total_seconds()
            cooldown = self.interruption_policy.cooldown_seconds(response.priority)
            if elapsed < cooldown:
                return self._policy_result(state, "defer", "cooldown")
        return self._policy_result(state, "show", "eligible")

    def render(self, state: PetGraphState) -> PetGraphState:
        response = state["response"]
        action = state["policy_action"]
        decision = PopupDecision(
            action=action,
            reason=state["policy_reason"],
            message=response.message if action == "show" else None,
            priority=response.priority,
            display_seconds={"low": 5, "normal": 7, "high": 10}[response.priority],
            dedupe_key=(
                f"pet:{state['situation'].value}:{response.intent}"
                if action == "show"
                else None
            ),
        )
        return {**state, "decision": decision}

    @staticmethod
    def _classified(
        state: PetGraphState,
        situation: PetSituation,
        confidence: float,
        source: PetClassificationSource,
    ) -> PetGraphState:
        return {
            **state,
            "situation": situation,
            "classification_confidence": confidence,
            "classification_source": source,
        }

    @staticmethod
    def _policy_result(
        state: PetGraphState,
        action: Literal["show", "defer", "drop"],
        reason: str,
    ) -> PetGraphState:
        return {
            **state,
            "policy_action": action,
            "policy_reason": reason,
        }


def _rule_situation(activity_hint: str | None) -> PetSituation:
    if activity_hint is None:
        return PetSituation.UNKNOWN
    normalized = activity_hint.strip().lower().replace("-", "_").replace(" ", "_")
    return _RULE_SITUATIONS.get(normalized, PetSituation.UNKNOWN)


def _should_avoid_catchphrase(candidate: str, recent_messages: list[str]) -> bool:
    if not _has_catchphrase(candidate) or not recent_messages:
        return False
    recent_five = recent_messages[-5:]
    repeated = any(
        stem in candidate and any(stem in previous for previous in recent_five)
        for stem in _CATCHPHRASE_STEMS
    )
    catchphrase_count = sum(_has_catchphrase(message) for message in recent_messages)
    catchphrase_budget_used = catchphrase_count * 2 >= len(recent_messages)
    return repeated or catchphrase_budget_used


def _has_catchphrase(message: str) -> bool:
    return any(stem in message for stem in _CATCHPHRASE_STEMS)
