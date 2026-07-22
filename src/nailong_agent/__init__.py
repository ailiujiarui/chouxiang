"""Desktop pet application boundary for Nailong Agent."""

from nailong_agent.events import (
    ActivityClassification,
    ActivityEvent,
    ActivitySnapshot,
    EventEnvelope,
    PersonalityResponseProposal,
    PopupDecision,
)
from nailong_agent.privacy import CollectionDecision, PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore

__all__ = [
    "ActivityClassification",
    "ActivityEvent",
    "ActivitySnapshot",
    "EventEnvelope",
    "PersonalityResponseProposal",
    "PopupDecision",
    "CollectionDecision",
    "PrivacyConsent",
    "PrivacyPolicy",
    "PrivacyStore",
]
