from __future__ import annotations

from pathlib import Path

from nailong_agent.activity_collector import ForegroundWindow, WindowActivityCollector
from nailong_agent.code_review import CodeReviewService
from nailong_agent.event_bus import EventBus
from nailong_agent.events import PetPreferences
from nailong_agent.privacy import PrivacyConsent, PrivacyPolicy
from nailong_agent.privacy_store import PrivacyStore
from nailong_agent.windows_activity import python_file_path_from_title



def test_privacy_store_round_trips_auto_code_review_consent(tmp_path: Path) -> None:
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    assert store.load_consent() is None

    store.save_consent(PrivacyConsent(activity_collection_enabled=True, auto_code_review_enabled=True))
    loaded = store.load_consent()
    assert loaded is not None
    assert loaded.activity_collection_enabled is True
    assert loaded.auto_code_review_enabled is True
    assert loaded.permits("auto_code_review") is True


def test_collector_triggers_code_review_callback_when_allowed(tmp_path: Path) -> None:
    reviewed: list[str] = []
    collector = WindowActivityCollector(
        source=_NullSource(),
        privacy_policy=PrivacyPolicy(
            PrivacyConsent(activity_collection_enabled=True, auto_code_review_enabled=True)
        ),
        privacy_store=PrivacyStore(tmp_path / "privacy.sqlite"),
        event_bus=EventBus(),
        preferences=lambda: PetPreferences(),
        application_rules=lambda: [],
        on_python_file=reviewed.append,
        code_review_enabled=lambda: True,
    )

    collector._on_foreground_change(
        ForegroundWindow(
            process_id=1,
            executable_name="Code.exe",
            ide_activity_hint="coding",
            python_file_path=r"C:\proj\app.py",
        )
    )

    assert reviewed == [r"C:\proj\app.py"]


def test_collector_skips_code_review_without_consent(tmp_path: Path) -> None:
    reviewed: list[str] = []
    collector = WindowActivityCollector(
        source=_NullSource(),
        privacy_policy=PrivacyPolicy(PrivacyConsent(activity_collection_enabled=True)),
        privacy_store=PrivacyStore(tmp_path / "privacy.sqlite"),
        event_bus=EventBus(),
        preferences=lambda: PetPreferences(),
        application_rules=lambda: [],
        on_python_file=reviewed.append,
        code_review_enabled=lambda: False,
    )

    collector._on_foreground_change(
        ForegroundWindow(
            process_id=1,
            executable_name="Code.exe",
            ide_activity_hint="coding",
            python_file_path=r"C:\proj\app.py",
        )
    )

    assert reviewed == []



class _NullSource:
    def start(self, on_change) -> None:
        return None

    def stop(self) -> None:
        return None
