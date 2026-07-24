from __future__ import annotations

from nailong_agent.contracts import PetDecisionInput, PetDecisionOutput
from nailong_agent.event_bus import EventBus
from nailong_agent.personality_agent import PetPersonalityAgent


class PetDecisionService:
    """Connect the personality graph to the event bus without coupling it to UI code."""

    def __init__(self, *, agent: PetPersonalityAgent, bus: EventBus) -> None:
        self.agent = agent
        self.bus = bus

    def decide_and_publish(self, decision_input: PetDecisionInput) -> PetDecisionOutput:
        decision = self.agent.decide(decision_input)
        self.bus.publish(decision.envelope())
        return decision
