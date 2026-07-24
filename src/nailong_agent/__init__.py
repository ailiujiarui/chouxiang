"""Desktop pet application boundary for Nailong Agent."""

from nailong_agent.analysis_subscriber import AnalysisEventSubscriber, HttpxSSEAnalysisEventSource
from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PetSituation,
    RedactedActivitySignal,
)
from nailong_agent.decision_service import PetDecisionService
from nailong_agent.events import (
    ActivityClassification,
    ActivityEvent,
    ActivitySnapshot,
    EventEnvelope,
    NotificationIngestReceipt,
    NotificationIntent,
    NotificationKind,
    NotificationStatus,
    PersonalityResponseProposal,
    PopupDecision,
)
from nailong_agent.notification_service import NotificationPort, NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.personality_agent import PetPersonalityAgent
from nailong_agent.pet_graph import PET_NODE_ORDER, render_pet_graph_mermaid, run_pet_graph
from nailong_agent.pet_state import (
    InterruptionPolicy,
    PersonalityIntensity,
    PetEmotion,
    PetGraphState,
)
from nailong_agent.privacy import CollectionDecision, PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore

__all__ = [
    "AnalysisEventSubscriber",
    "ActivityClassification",
    "ActivityEvent",
    "ActivitySnapshot",
    "CollectionDecision",
    "EventEnvelope",
    "HttpxSSEAnalysisEventSource",
    "InterruptionPolicy",
    "NotificationIngestReceipt",
    "NotificationIntent",
    "NotificationKind",
    "NotificationPort",
    "NotificationService",
    "NotificationStatus",
    "NotificationStore",
    "PET_NODE_ORDER",
    "PetClassificationHint",
    "PetDecisionContext",
    "PetDecisionInput",
    "PetDecisionOutput",
    "PetDecisionService",
    "PetEmotion",
    "PetGraphState",
    "PetPersonalityAgent",
    "PetSituation",
    "PersonalityIntensity",
    "PersonalityResponseProposal",
    "PopupDecision",
    "PrivacyConsent",
    "PrivacyPolicy",
    "PrivacyStore",
    "RedactedActivitySignal",
    "render_pet_graph_mermaid",
    "run_pet_graph",
]
