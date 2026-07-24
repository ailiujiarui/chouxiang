"""Desktop pet application boundary for Nailong Agent."""

from nailong_agent.analysis_subscriber import AnalysisEventSubscriber, HttpxSSEAnalysisEventSource
from nailong_agent.activity_aggregator import ActivityEventAggregator
from nailong_agent.config import NailongSettings
from nailong_agent.contracts import (
    PetClassificationHint,
    PetDecisionContext,
    PetDecisionInput,
    PetDecisionOutput,
    PetPersonalityResponse,
    PersonalityScenario,
    RedactedActivitySignal,
)
from nailong_agent.events import (
    ActivityClassification,
    ActivityEvent,
    ActivitySnapshot,
    ActivityType,
    ActivityWindow,
    EventEnvelope,
    NotificationIngestReceipt,
    NotificationIntent,
    NotificationKind,
    NotificationStatus,
    PersonalityResponseProposal,
    PetExpression,
    PetState,
    PopupDecision,
    RawActivitySignal,
)
from nailong_agent.notification_service import NotificationPort, NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.personality_agent import PetPersonalityAgent
from nailong_agent.pet_graph import PET_NODE_ORDER, run_pet_graph
from nailong_agent.pet_state import (
    PersonalityIntensity,
    PetEmotion,
    PetGraphState,
)
from nailong_agent.privacy import CollectionDecision, PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore

__all__ = [
    "AnalysisEventSubscriber",
    "ActivityEventAggregator",
    "ActivityClassification",
    "ActivityEvent",
    "ActivitySnapshot",
    "ActivityType",
    "ActivityWindow",
    "CollectionDecision",
    "EventEnvelope",
    "HttpxSSEAnalysisEventSource",
    "NotificationIngestReceipt",
    "NotificationIntent",
    "NotificationKind",
    "NotificationPort",
    "NotificationService",
    "NotificationStatus",
    "NotificationStore",
    "NailongSettings",
    "PET_NODE_ORDER",
    "PetClassificationHint",
    "PetDecisionContext",
    "PetDecisionInput",
    "PetDecisionOutput",
    "PetEmotion",
    "PetGraphState",
    "PetPersonalityAgent",
    "PetPersonalityResponse",
    "PersonalityScenario",
    "PersonalityIntensity",
    "PersonalityResponseProposal",
    "PetExpression",
    "PetState",
    "PopupDecision",
    "RawActivitySignal",
    "PrivacyConsent",
    "PrivacyPolicy",
    "PrivacyStore",
    "RedactedActivitySignal",
    "run_pet_graph",
]
