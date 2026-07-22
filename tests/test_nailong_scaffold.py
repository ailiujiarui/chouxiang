from __future__ import annotations

from threading import Event

import pytest

from nailong_agent.app import DesktopProcess, SingleInstanceLock, main
from nailong_agent.event_bus import EventBus, EventBusError
from nailong_agent.events import ActivityEvent, EventEnvelope, PopupDecision
from nailong_agent.renderer import NullRenderer
from nailong_agent.privacy import PrivacyConsent
from nailong_agent.privacy_store import PrivacyStore


def test_event_models_create_serializable_envelopes() -> None:
    activity = ActivityEvent(source="window", application_id="code", activity_hint="editing")

    envelope = activity.envelope()

    assert isinstance(envelope, EventEnvelope)
    assert envelope.event_type == "ActivityEvent"
    assert envelope.event_id == activity.event_id
    assert envelope.occurred_at == activity.occurred_at
    assert envelope.payload["application_id"] == "code"
    assert envelope.source == "window"


def test_event_bus_delivers_typed_and_wildcard_subscribers() -> None:
    bus = EventBus()
    typed: list[str] = []
    wildcard: list[str] = []
    bus.subscribe("PopupDecision", lambda event: typed.append(event.payload["reason"]))
    bus.subscribe("*", lambda event: wildcard.append(event.event_type))
    bus.start()

    assert bus.publish(PopupDecision(action="show", reason="test", message="hello").envelope())
    assert bus.wait_idle(1.0)
    bus.stop()

    assert typed == ["test"]
    assert wildcard == ["PopupDecision"]


def test_event_bus_isolates_handler_failures() -> None:
    errors: list[str] = []
    handled = Event()
    bus = EventBus(on_error=lambda error, _: errors.append(str(error)))
    bus.subscribe("PopupDecision", lambda _: (_ for _ in ()).throw(RuntimeError("broken handler")))
    bus.subscribe("PopupDecision", lambda _: handled.set())
    bus.start()

    bus.publish(PopupDecision(action="drop", reason="test").envelope())
    assert bus.wait_idle(1.0)
    bus.stop()

    assert handled.is_set()
    assert errors == ["broken handler"]


def test_event_bus_rejects_publish_after_stop() -> None:
    bus = EventBus()
    bus.start()
    bus.stop()

    with pytest.raises(EventBusError):
        bus.publish(PopupDecision(action="drop", reason="closed").envelope())


def test_null_renderer_only_records_visible_decisions() -> None:
    renderer = NullRenderer()
    renderer.start()
    renderer.show(PopupDecision(action="defer", reason="fullscreen", message="later"))
    renderer.show(PopupDecision(action="show", reason="ready", message="now"))
    renderer.stop()

    assert [decision.message for decision in renderer.decisions] == ["now"]
    assert renderer.started is False


def test_single_instance_lock_rejects_second_owner(tmp_path) -> None:
    path = tmp_path / "nailong.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_desktop_process_headless_lifecycle(tmp_path) -> None:
    renderer = NullRenderer()
    process = DesktopProcess(lock_path=tmp_path / "nailong.lock", renderer_factory=lambda: renderer)

    assert process.run() == 0
    assert renderer.started is False


def test_desktop_process_requests_and_persists_first_startup_consent(tmp_path) -> None:
    renderer = NullRenderer()
    renderer.consent_response = PrivacyConsent(activity_collection_enabled=True)
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=lambda: renderer,
        privacy_store=store,
    )

    assert process.run() == 0
    assert renderer.consent_requested is True
    assert store.load_consent() == renderer.consent_response


def test_desktop_process_keeps_legacy_renderers_compatible_and_fail_closed(tmp_path) -> None:
    class LegacyRenderer(NullRenderer):
        request_privacy_consent = None
        configure_privacy_controls = None

    store = PrivacyStore(tmp_path / "privacy.sqlite")
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=LegacyRenderer,
        privacy_store=store,
    )

    assert process.run() == 0
    assert store.load_consent() == PrivacyConsent()


def test_module_entrypoint_supports_headless_mode(tmp_path) -> None:
    assert main(["--headless", "--lock-path", str(tmp_path / "nailong.lock")]) == 0
