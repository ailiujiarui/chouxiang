"""Desktop pet application boundary for Nailong Agent."""

from nailong_agent.analysis_subscriber import AnalysisEventSubscriber, HttpxSSEAnalysisEventSource
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
    PetExpression,
    PetState,
    PopupDecision,
)
from nailong_agent.notification_service import NotificationPort, NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.privacy import CollectionDecision, PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore

__all__ = [
    "AnalysisEventSubscriber",
    "ActivityClassification",
    "ActivityEvent",
    "ActivitySnapshot",
    "EventEnvelope",
    "NotificationIngestReceipt",
    "NotificationIntent",
    "NotificationKind",
    "NotificationPort",
    "NotificationService",
    "NotificationStatus",
    "NotificationStore",
    "HttpxSSEAnalysisEventSource",
    "PersonalityResponseProposal",
    "PetExpression",
    "PetState",
    "PopupDecision",
    "CollectionDecision",
    "PrivacyConsent",
    "PrivacyPolicy",
    "PrivacyStore",
]
