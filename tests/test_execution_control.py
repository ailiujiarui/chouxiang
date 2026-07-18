from datetime import datetime, timedelta, timezone

import pytest

from refactor_agent.execution_control import (
    ExecutionCancelled,
    ExecutionControl,
    ExecutionDeadlineExceeded,
)


def test_execution_control_raises_at_cancelled_checkpoint():
    control = ExecutionControl(
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        is_cancel_requested=lambda: True,
    )

    with pytest.raises(ExecutionCancelled, match="before-minimizer"):
        control.checkpoint("before-minimizer")


def test_execution_control_rejects_expired_deadline():
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    control = ExecutionControl(
        deadline_at=now - timedelta(seconds=1),
        clock=lambda: now,
    )

    assert control.remaining_seconds() == 0
    with pytest.raises(ExecutionDeadlineExceeded, match="before-pytest"):
        control.checkpoint("before-pytest")


def test_execution_control_bounds_component_timeout_to_remaining_deadline():
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    control = ExecutionControl(
        deadline_at=now + timedelta(seconds=12.5),
        clock=lambda: now,
    )

    assert control.bounded_timeout(30, "pytest") == 12.5
    assert control.bounded_timeout(5, "github-http") == 5


def test_execution_control_requires_timezone_aware_deadline():
    with pytest.raises(ValueError, match="timezone-aware"):
        ExecutionControl(deadline_at=datetime(2026, 7, 14))


def test_bounded_timeout_raises_if_deadline_expires_between_checks():
    start = datetime(2026, 7, 14, tzinfo=timezone.utc)
    moments = iter([start, start + timedelta(seconds=2)])
    control = ExecutionControl(
        deadline_at=start + timedelta(seconds=1),
        clock=lambda: next(moments),
    )

    with pytest.raises(ExecutionDeadlineExceeded, match="before-pytest"):
        control.bounded_timeout(30, "pytest")
