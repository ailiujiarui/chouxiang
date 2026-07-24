from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nailong_agent.contracts import (
    PetClassificationSource,
    PetDecisionInput,
    PetDecisionOutput,
    PersonalityScenario,
)
from nailong_agent.events import PersonalityResponseProposal
from nailong_agent.pet_graph import run_pet_graph
from nailong_agent.pet_prompts import (
    PET_PERSONALITY_SYSTEM_PROMPT,
    build_pet_personality_user_prompt,
)
from nailong_agent.pet_state import (
    PersonalityIntensity,
    PetEmotion,
    PetGraphState,
)
from refactor_agent.llm import LLMProvider


class _LLMPersonalityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=500)


_EMOTIONS = {
    PersonalityScenario.CODING: PetEmotion.CURIOUS,
    PersonalityScenario.DEBUGGING: PetEmotion.CONCERNED,
    PersonalityScenario.TEST_FAILED: PetEmotion.CONCERNED,
    PersonalityScenario.TEST_SUCCEEDED: PetEmotion.CELEBRATING,
    PersonalityScenario.COMPILE_SUCCEEDED: PetEmotion.CHEERFUL,
    PersonalityScenario.LONG_WORK: PetEmotion.SLEEPY,
    PersonalityScenario.IDLE: PetEmotion.SLEEPY,
    PersonalityScenario.MEETING: PetEmotion.NEUTRAL,
    PersonalityScenario.ENTERTAINMENT: PetEmotion.CHEERFUL,
    PersonalityScenario.UNKNOWN: PetEmotion.NEUTRAL,
}

_SCENARIO_BY_ACTIVITY_LABEL = {
    "coding": PersonalityScenario.CODING,
    "editing_code": PersonalityScenario.CODING,
    "debug": PersonalityScenario.DEBUGGING,
    "debugging": PersonalityScenario.DEBUGGING,
    "debug_session": PersonalityScenario.DEBUGGING,
    "test_failed": PersonalityScenario.TEST_FAILED,
    "tests_failed": PersonalityScenario.TEST_FAILED,
    "pytest_failed": PersonalityScenario.TEST_FAILED,
    "test_succeeded": PersonalityScenario.TEST_SUCCEEDED,
    "tests_succeeded": PersonalityScenario.TEST_SUCCEEDED,
    "tests_passed": PersonalityScenario.TEST_SUCCEEDED,
    "pytest_passed": PersonalityScenario.TEST_SUCCEEDED,
    "compile_succeeded": PersonalityScenario.COMPILE_SUCCEEDED,
    "build_succeeded": PersonalityScenario.COMPILE_SUCCEEDED,
    "long_work": PersonalityScenario.LONG_WORK,
    "idle": PersonalityScenario.IDLE,
    "meeting": PersonalityScenario.MEETING,
    "entertainment": PersonalityScenario.ENTERTAINMENT,
    "gaming": PersonalityScenario.ENTERTAINMENT,
    "media": PersonalityScenario.ENTERTAINMENT,
}

_MESSAGES: dict[PersonalityIntensity, dict[PersonalityScenario, str]] = {
    PersonalityIntensity.LOW: {
        PersonalityScenario.DEBUGGING: "看起来还在调试。先看最近一次变化，本龙陪你理一理。",
        PersonalityScenario.TEST_FAILED: "测试没有通过。先看第一条失败，本龙陪你一起排查。",
        PersonalityScenario.TEST_SUCCEEDED: "测试通过了。本龙也替你高兴。",
        PersonalityScenario.COMPILE_SUCCEEDED: "编译通过了，记得继续确认测试结果。",
        PersonalityScenario.LONG_WORK: "你已经忙一阵了。本龙陪你休息一下再继续。",
    },
    PersonalityIntensity.STANDARD: {
        PersonalityScenario.DEBUGGING: "哼，这个问题还挺会躲。本龙只是顺手陪你从最近一次变化开始看。",
        PersonalityScenario.TEST_FAILED: "哼，这个测试又闹脾气了。本龙还没认输，先看第一条失败。",
        PersonalityScenario.TEST_SUCCEEDED: "看吧，还得是本龙……和你也有那么一点功劳。",
        PersonalityScenario.COMPILE_SUCCEEDED: "编译通过啦，勉强有本龙几分风范。下一步再确认测试。",
        PersonalityScenario.LONG_WORK: "你已经忙很久了。本龙才不是担心你，起来喝口水再继续？",
    },
    PersonalityIntensity.HIGH: {
        PersonalityScenario.DEBUGGING: "哼，这个问题躲得倒挺快，可躲不过本龙的龙角！先从最近一次变化查起。",
        PersonalityScenario.TEST_FAILED: "哼，这个测试还敢闹脾气？本龙可没认输，先揪住第一条失败。",
        PersonalityScenario.TEST_SUCCEEDED: "看吧，还得是本龙……咳，你也确实干得漂亮！",
        PersonalityScenario.COMPILE_SUCCEEDED: "编译通过啦！勉强追上本龙甩尾巴的速度，下一步再确认测试。",
        PersonalityScenario.LONG_WORK: "忙这么久，连本龙的零食都要放凉了。本龙才不是担心你，先喝口水！",
    },
}

_CATCHPHRASE_FREE_MESSAGES = {
    PersonalityScenario.DEBUGGING: "这个问题躲得挺快，龙角都快被它绕晕了。先查最近一次变化。",
    PersonalityScenario.TEST_FAILED: "这个测试又把尾巴翘起来了。先抓第一条失败，后面的噪声等等。",
    PersonalityScenario.TEST_SUCCEEDED: "测试确实通过了，小爪子都忍不住要鼓掌。",
    PersonalityScenario.COMPILE_SUCCEEDED: "编译已经通过，龙角接收到好消息了。下一步再确认测试。",
    PersonalityScenario.LONG_WORK: "忙了这么久，连零食都该歇一会儿。先喝口水再继续？",
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

_INTENTS: dict[
    PersonalityScenario,
    Literal["encourage", "remind", "celebrate", "ask", "stay_silent"],
] = {
    PersonalityScenario.CODING: "stay_silent",
    PersonalityScenario.DEBUGGING: "encourage",
    PersonalityScenario.TEST_FAILED: "remind",
    PersonalityScenario.TEST_SUCCEEDED: "celebrate",
    PersonalityScenario.COMPILE_SUCCEEDED: "celebrate",
    PersonalityScenario.LONG_WORK: "remind",
    PersonalityScenario.IDLE: "stay_silent",
    PersonalityScenario.MEETING: "stay_silent",
    PersonalityScenario.ENTERTAINMENT: "stay_silent",
    PersonalityScenario.UNKNOWN: "stay_silent",
}


class PetPersonalityAgent:
    """Personality graph that treats upstream activity classification as authoritative."""

    def __init__(
        self,
        *,
        provider: LLMProvider | None = None,
        intensity: PersonalityIntensity | str = PersonalityIntensity.STANDARD,
        response_confidence_threshold: float = 0.65,
    ) -> None:
        if not 0.0 <= response_confidence_threshold <= 1.0:
            raise ValueError("response_confidence_threshold must be between 0 and 1")
        self.provider = provider
        self.intensity = PersonalityIntensity(intensity)
        self.response_confidence_threshold = response_confidence_threshold

    def decide(self, decision_input: PetDecisionInput) -> PetDecisionOutput:
        return self.run(decision_input)["output"]

    def run(self, decision_input: PetDecisionInput) -> PetGraphState:
        validated = PetDecisionInput.model_validate(decision_input)
        return run_pet_graph(
            {"decision_input": validated, "node_trace": []},
            self,
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
                PersonalityScenario.UNKNOWN,
                0.0,
                "collector",
            )

        if provided is None:
            return self._classified(
                state,
                PersonalityScenario.UNKNOWN,
                0.0,
                "unavailable",
            )

        return self._classified(
            state,
            _scenario_for_activity_label(provided.activity),
            provided.confidence,
            provided.classifier,
        )

    def infer_emotion(self, state: PetGraphState) -> PetGraphState:
        return {
            **state,
            "emotion": _EMOTIONS[state["scenario"]],
        }

    def choose_personality_response(self, state: PetGraphState) -> PetGraphState:
        scenario = state["scenario"]
        signal = state["signal"]
        confidence = state["classification_confidence"]
        intent = _INTENTS[scenario]
        message = _MESSAGES[self.intensity].get(scenario, "保持安静")
        if scenario in _CATCHPHRASE_FREE_MESSAGES and _should_avoid_catchphrase(
            message,
            state["context"].recent_messages,
        ):
            message = _CATCHPHRASE_FREE_MESSAGES[scenario]

        if confidence < self.response_confidence_threshold:
            intent = "stay_silent"
            message = "信息不足，保持安静"
        if signal.source == "user" and intent == "stay_silent":
            intent = "ask"
            message = "哼，本龙听见了。本龙先不乱猜，你想让本龙陪你看什么？"

        llm_update: dict[str, bool | str | None] = {}
        if (
            self.provider is not None
            and signal.sensitivity == "public"
            and intent != "stay_silent"
        ):
            try:
                raw_result = self.provider.complete_json(
                    system_prompt=PET_PERSONALITY_SYSTEM_PROMPT,
                    user_prompt=build_pet_personality_user_prompt(
                        signal=signal,
                        scenario=scenario,
                        emotion=state["emotion"],
                        intent=intent,
                        intensity=self.intensity,
                        fallback_message=message,
                    ),
                    temperature=0.6,
                )
                generated = _LLMPersonalityResponse.model_validate(raw_result)
                if _echoes_untrusted_summary(
                    generated.message,
                    signal.redacted_summary,
                ):
                    raise ValueError("LLM response echoed untrusted summary")
                message = generated.message
                llm_update = {"llm_used": True, "llm_error": None}
            except Exception as exc:
                llm_update = {
                    "llm_used": True,
                    "llm_error": type(exc).__name__,
                }

        response = PersonalityResponseProposal(
            persona_version=f"nailong-v1.1-{self.intensity.value}",
            emotion=state["emotion"].value,
            message=message,
            intent=intent,
        )
        return {**state, **llm_update, "response": response}

    def apply_interruption_policy(self, state: PetGraphState) -> PetGraphState:
        signal = state["signal"]
        response = state["response"]

        if signal.sensitivity != "public":
            return self._policy_result(state, "drop", "sensitive_activity")
        if response.intent == "stay_silent":
            return self._policy_result(state, "drop", "personality_chose_silence")
        return self._policy_result(state, "show", "personality_response_ready")

    def render(self, state: PetGraphState) -> PetGraphState:
        output = state["response"] if state["policy_action"] == "show" else None
        return {**state, "output": output}

    @staticmethod
    def _classified(
        state: PetGraphState,
        scenario: PersonalityScenario,
        confidence: float,
        source: PetClassificationSource,
    ) -> PetGraphState:
        return {
            **state,
            "scenario": scenario,
            "classification_confidence": confidence,
            "classification_source": source,
        }

    @staticmethod
    def _policy_result(
        state: PetGraphState,
        action: Literal["show", "drop"],
        reason: str,
    ) -> PetGraphState:
        return {
            **state,
            "policy_action": action,
            "policy_reason": reason,
        }


def _scenario_for_activity_label(activity_label: str | None) -> PersonalityScenario:
    if activity_label is None:
        return PersonalityScenario.UNKNOWN
    normalized = activity_label.strip().lower().replace("-", "_").replace(" ", "_")
    return _SCENARIO_BY_ACTIVITY_LABEL.get(normalized, PersonalityScenario.UNKNOWN)


def _echoes_untrusted_summary(message: str, summary: str | None) -> bool:
    if summary is None:
        return False
    normalized_summary = summary.strip().casefold()
    return len(normalized_summary) >= 8 and normalized_summary in message.casefold()


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
