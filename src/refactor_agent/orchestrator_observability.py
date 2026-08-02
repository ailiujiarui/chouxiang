from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from refactor_agent.analysis_events import (
    AnalysisEvent,
    AnalysisEventSink,
    AnalysisEventType,
    SafeMetric,
)
from refactor_agent.models import RewardBreakdown, TrajectoryStep
from refactor_agent.trajectory import append_trajectory


logger = logging.getLogger(__name__)


class OrchestratorObservability:
    """Record one workflow's trajectory and sanitized analysis events."""

    def __init__(
        self,
        *,
        trajectory_path: Path,
        analysis_event_sink: AnalysisEventSink,
        task_id: str,
        run_id: str,
        evidence_level: str,
    ) -> None:
        self.trajectory_path = trajectory_path
        self.analysis_event_sink = analysis_event_sink
        self.task_id = task_id
        self.run_id = run_id
        self.evidence_level = evidence_level

    def record_trajectory(
        self,
        *,
        attempt: int,
        status: str,
        message: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
        reward: RewardBreakdown | None = None,
    ) -> None:
        append_trajectory(
            self.trajectory_path,
            TrajectoryStep(
                attempt=attempt,
                status=status,
                message=message,
                agent=agent,
                metadata=metadata or {},
                reward=reward,
            ),
        )

    def emit_analysis_event(
        self,
        event_type: AnalysisEventType,
        *,
        attempt: int,
        phase: str | None = None,
        error_category: str | None = None,
        recoverable: bool | None = None,
        safe_metrics: dict[str, SafeMetric] | None = None,
    ) -> None:
        try:
            self.analysis_event_sink.emit(
                AnalysisEvent(
                    event_type=event_type,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    source="orchestrator",
                    phase=phase,
                    attempt=attempt,
                    evidence_level=self.evidence_level,
                    error_category=error_category,
                    recoverable=recoverable,
                    safe_metrics=safe_metrics or {},
                )
            )
        except Exception:
            logger.exception("Analysis event emission failed for run %s", self.run_id)
