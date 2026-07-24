from __future__ import annotations

from typing import Protocol, cast

from langgraph.graph import END, START, StateGraph

from nailong_agent.pet_state import PetGraphState


class PetDecisionNodes(Protocol):
    def observe(self, state: PetGraphState) -> PetGraphState: ...

    def classify(self, state: PetGraphState) -> PetGraphState: ...

    def infer_emotion(self, state: PetGraphState) -> PetGraphState: ...

    def choose_personality_response(self, state: PetGraphState) -> PetGraphState: ...

    def apply_interruption_policy(self, state: PetGraphState) -> PetGraphState: ...

    def render(self, state: PetGraphState) -> PetGraphState: ...


PET_NODE_ORDER = (
    "observe",
    "classify",
    "infer_emotion",
    "choose_personality_response",
    "apply_interruption_policy",
    "render",
)


def run_pet_graph(
    initial: PetGraphState,
    nodes: PetDecisionNodes,
) -> PetGraphState:
    graph = StateGraph(PetGraphState)
    for name in PET_NODE_ORDER:
        graph.add_node(name, _wrapped(name, getattr(nodes, name)))
    graph.add_edge(START, PET_NODE_ORDER[0])
    for source, target in zip(PET_NODE_ORDER, PET_NODE_ORDER[1:]):
        graph.add_edge(source, target)
    graph.add_edge(PET_NODE_ORDER[-1], END)
    return cast(PetGraphState, graph.compile().invoke(initial))


def _wrapped(name: str, node):
    def invoke(state: PetGraphState) -> PetGraphState:
        updated = node(dict(state))
        updated["node_trace"] = [*state.get("node_trace", []), name]
        return updated

    return invoke
