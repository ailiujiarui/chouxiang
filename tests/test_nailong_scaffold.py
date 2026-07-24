from __future__ import annotations

from datetime import datetime, timezone
from threading import Event

import pytest

from nailong_agent.app import DesktopProcess, SingleInstanceLock, main
from nailong_agent.event_bus import EventBus, EventBusError
from nailong_agent.events import ActivityEvent, EventEnvelope, PetExpression, PopupDecision
from nailong_agent.notification_policy import NotificationPolicy
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.renderer import (
    MOUTH_TEXT,
    NullRenderer,
    PySide6Renderer,
    decision_to_pet_state,
    place_bubble_above_pet,
)
from nailong_agent.privacy import PrivacyConsent
from nailong_agent.privacy_store import PrivacyStore
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType


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


def test_bubble_is_clamped_above_pet_without_covering_it() -> None:
    placement = place_bubble_above_pet(
        available=(0, 0, 1280, 720),
        pet=(1076, 576, 180, 120),
        bubble_size=(420, 300),
    )

    assert placement.x >= 12
    assert placement.x + 420 <= 1280 - 12
    assert placement.y >= 12
    assert placement.y + 300 <= 576 - 6
    assert 28 <= placement.tail_x <= 420 - 28


def test_pyside_bubble_wraps_long_text_and_stays_above_pet(monkeypatch) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QColor, QPalette, QTextCursor

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    renderer = PySide6Renderer()
    original_palette = renderer._app.palette()
    dark_palette = QPalette(original_palette)
    dark_palette.setColor(QPalette.Text, QColor("#FFFFFF"))
    dark_palette.setColor(QPalette.WindowText, QColor("#FFFFFF"))
    renderer._app.setPalette(dark_palette)
    renderer.start()
    try:
        message = "测试通过，但还需要等待最终裁决。" * 30
        assert renderer.show(PopupDecision(action="show", reason="layout", message=message)) is True
        renderer._app.processEvents()

        popup = renderer._popups[-1]
        text_cursor = QTextCursor(popup._document)
        text_cursor.movePosition(QTextCursor.Start)
        assert text_cursor.charFormat().foreground().color().name() == "#171717"
        popup_geometry = popup.frameGeometry()
        pet_geometry = renderer._pet_window.frameGeometry()
        screen = renderer._QApplication.screenAt(pet_geometry.center()) or renderer._app.primaryScreen()
        assert screen is not None
        area = screen.availableGeometry()
        assert popup_geometry.top() >= area.top() + 12
        assert popup_geometry.left() >= area.left() + 12
        assert popup_geometry.right() <= area.right() - 12
        assert popup_geometry.bottom() <= pet_geometry.top() - 6
        assert popup.width() <= 420
        assert popup.height() > 58
        available_text_height = (
            popup.height()
            - popup._tail_height
            - popup._top_padding
            - popup._bottom_padding
            - popup._border_inset * 2
        )
        assert popup._document.size().height() <= available_text_height + 1
        image = popup.grab().toImage()
        dark_text_pixels = 0
        for y in range(popup._top_padding, popup.height() - popup._tail_height - popup._bottom_padding):
            for x in range(popup._horizontal_padding, popup.width() - popup._horizontal_padding):
                color = image.pixelColor(x, y)
                if color.alpha() > 0 and max(color.red(), color.green(), color.blue()) < 80:
                    dark_text_pixels += 1
        assert dark_text_pixels > 20
    finally:
        renderer._app.setPalette(original_palette)
        renderer.stop()
def test_literal_face_labels_and_expression_mapping() -> None:
    assert MOUTH_TEXT[PetExpression.NEUTRAL] == "（嘴巴）"
    assert MOUTH_TEXT[PetExpression.LAUGH] == "（大笑）"
    assert MOUTH_TEXT[PetExpression.CONCERNED] == "（担忧）"
    assert decision_to_pet_state(
        PopupDecision(action="show", reason="analysis-complete", message="（完成）")
    ).expression == PetExpression.LAUGH
    assert decision_to_pet_state(
        PopupDecision(action="show", reason="analysis-failed", message="（失败）")
    ).expression == PetExpression.CONCERNED


def test_null_renderer_records_mapped_state() -> None:
    renderer = NullRenderer()
    renderer.show(PopupDecision(action="show", reason="ready", message="（工作中）"))
    assert renderer.states[-1].expression == PetExpression.HAPPY
    assert renderer.states[-1].bubble_text == "（工作中）"


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


def test_desktop_process_delivers_durable_intent_and_acknowledges_renderer_handoff(tmp_path) -> None:
    shown = Event()

    class WaitingRenderer(NullRenderer):
        def show(self, decision: PopupDecision) -> bool:
            accepted = super().show(decision)
            shown.set()
            return accepted

        def exec(self) -> int:
            assert shown.wait(2.0)
            return 0

    renderer = WaitingRenderer()
    notifications = NotificationService(
        store=NotificationStore(tmp_path / "notifications.sqlite"),
        policy=NotificationPolicy(minimum_cooldown_seconds=300, maximum_cooldown_seconds=300),
        clock=lambda: datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),
    )
    notifications.ingest_analysis_event(
        AnalysisEvent(
            sequence=1,
            event_type=AnalysisEventType.FINAL_VERDICT_PASSED,
            task_id="task-1",
            source="orchestrator",
        )
    )
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=lambda: renderer,
        notification_service=notifications,
    )

    assert process.run() == 0
    assert len(renderer.decisions) == 1
    assert renderer.decisions[0].dedupe_key is not None
    assert notifications.get_status().pending_count == 0


def test_renderer_all_day_dnd_control_updates_durable_notification_state(tmp_path) -> None:
    renderer = NullRenderer()
    notifications = NotificationService.from_database(tmp_path / "notifications.sqlite")
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=lambda: renderer,
        notification_service=notifications,
    )
    assert process.run() == 0

    renderer.set_do_not_disturb(True)
    assert notifications.get_status().do_not_disturb is True
    renderer.set_do_not_disturb(False)
    assert notifications.get_status().do_not_disturb is False


def test_render_boundary_dismisses_an_already_leased_popup_when_dnd_turns_on(tmp_path) -> None:
    renderer = NullRenderer()
    notifications = NotificationService.from_database(tmp_path / "notifications.sqlite")
    notifications.ingest_analysis_event(
        AnalysisEvent(
            sequence=1,
            event_type=AnalysisEventType.TASK_FAILED,
            task_id="task-1",
            source="worker",
        )
    )
    leased = notifications.lease_next()
    assert leased is not None
    notifications.set_do_not_disturb(True)
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=lambda: renderer,
        notification_service=notifications,
    )
    process.renderer = renderer

    process._render_popup(
        PopupDecision(
            action="show",
            reason="race-test",
            message=leased.message,
            dedupe_key=leased.notification_id,
        ).envelope()
    )

    assert renderer.decisions == []
    assert notifications.get_status().pending_count == 0


def test_desktop_process_starts_and_stops_injected_analysis_subscriber(tmp_path) -> None:
    class SubscriberProbe:
        starts = 0
        stops = 0

        def start(self) -> None:
            self.starts += 1

        def stop(self) -> None:
            self.stops += 1

    subscriber = SubscriberProbe()
    process = DesktopProcess(
        lock_path=tmp_path / "nailong.lock",
        renderer_factory=NullRenderer,
        analysis_subscriber=subscriber,  # type: ignore[arg-type]
    )

    assert process.run() == 0
    assert (subscriber.starts, subscriber.stops) == (1, 1)


def test_module_entrypoint_supports_headless_mode(tmp_path) -> None:
    assert main(["--headless", "--lock-path", str(tmp_path / "nailong.lock")]) == 0
