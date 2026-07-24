"""Trigger a deterministic proactive-notification smoke case.

Default mode runs the notification service, delivery pump, EventBus, and
NullRenderer in one process.  ``--live`` writes a synthetic terminal event to
the shared desktop notification database so a running Nailong process can
display the popup through its normal delivery pump.
"""

from __future__ import annotations

import argparse
import gc
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from nailong_agent.app import DesktopProcess
from nailong_agent.delivery import NotificationDeliveryPump
from nailong_agent.event_bus import EventBus
from nailong_agent.events import PopupDecision
from nailong_agent.notification_policy import NotificationPolicy
from nailong_agent.notification_service import NotificationService
from nailong_agent.notification_store import NotificationStore
from nailong_agent.privacy_store import PrivacyStore
from nailong_agent.renderer import NullRenderer
from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType


def _event(event_type: AnalysisEventType, task_id: str) -> AnalysisEvent:
    return AnalysisEvent(
        event_id=f"notification-demo-{uuid4().hex}",
        event_type=event_type,
        task_id=task_id,
        source="system",
        occurred_at=datetime.now(timezone.utc),
    )


def _service(database: Path) -> NotificationService:
    # A zero cooldown makes the smoke case deterministic and independent of
    # notification history left by a previous manual run.
    return NotificationService(
        store=NotificationStore(database),
        policy=NotificationPolicy(minimum_cooldown_seconds=0, maximum_cooldown_seconds=0),
    )


def run_headless(database: Path) -> dict[str, object]:
    service = _service(database)
    bus = EventBus()
    renderer = NullRenderer()
    process = DesktopProcess(
        lock_path=database.with_suffix(".lock"),
        bus=bus,
        renderer_factory=lambda: renderer,
        privacy_store=PrivacyStore(database.with_name(f"{database.stem}-privacy.sqlite")),
        notification_service=service,
    )
    process.renderer = renderer
    bus.subscribe("PopupDecision", process._render_popup)
    bus.start()
    renderer.start()
    try:
        event = _event(AnalysisEventType.TASK_STARTED, "notification-demo-task")
        receipt = service.ingest_analysis_event(event)
        published = NotificationDeliveryPump(notifications=service, bus=bus).run_once()
        idle = bus.wait_idle(1.0)
        status = service.get_status()
        if (
            not receipt.notification_id
            or receipt.reason != "notification_enqueued"
            or not published
            or not idle
            or len(renderer.decisions) != 1
            or status.pending_count != 0
        ):
            raise RuntimeError(
                "notification smoke case did not reach the renderer: "
                f"receipt={receipt.model_dump()}, published={published}, "
                f"idle={idle}, decisions={len(renderer.decisions)}, status={status.model_dump()}"
            )
        decision = renderer.decisions[0]
        return {
            "mode": "headless",
            "event_type": event.event_type,
            "receipt_reason": receipt.reason,
            "published": published,
            "renderer_popup_count": len(renderer.decisions),
            "popup_reason": decision.reason,
            "popup_message": decision.message,
            "acknowledged_pending_count": status.pending_count,
        }
    finally:
        bus.stop()
        renderer.stop()


def enqueue_live(database: Path) -> dict[str, object]:
    service = _service(database)
    task_id = f"notification-demo-live-{uuid4().hex[:8]}"
    event = _event(AnalysisEventType.FINAL_VERDICT_PASSED, task_id)
    receipt = service.ingest_analysis_event(event)
    if not receipt.notification_id:
        raise RuntimeError(f"live notification was not enqueued: {receipt.model_dump()}")
    return {
        "mode": "live",
        "event_type": event.event_type,
        "task_id": task_id,
        "receipt_reason": receipt.reason,
        "notification_id": receipt.notification_id,
        "database": str(database.resolve()),
        "hint": "A running Nailong desktop using this database should show the popup within about one second.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trigger a proactive notification smoke case")
    parser.add_argument(
        "--database",
        type=Path,
        help="notification SQLite path; default headless mode uses a temporary .runs database",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="enqueue a terminal event for a running Nailong desktop process",
    )
    args = parser.parse_args(argv)

    temporary = args.database is None and not args.live
    database = args.database or Path(".runs") / f"notification-demo-{uuid4().hex}.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    privacy_database = database.with_name(f"{database.stem}-privacy.sqlite")
    try:
        result = enqueue_live(database) if args.live else run_headless(database)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        if temporary:
            # sqlite3 connections are opened by the store per operation.  On
            # Windows, collect them before unlinking the short-lived demo DB.
            gc.collect()
            for path in (database, privacy_database, database.with_suffix(".lock")):
                try:
                    path.unlink(missing_ok=True)
                except PermissionError:
                    gc.collect()
                    path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
