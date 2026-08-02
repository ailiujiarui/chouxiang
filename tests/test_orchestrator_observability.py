import json
import logging
from pathlib import Path

from refactor_agent.analysis_events import AnalysisEvent, AnalysisEventType, PublishReceipt
from refactor_agent.models import RewardBreakdown
from refactor_agent.orchestrator_observability import OrchestratorObservability


class CapturingSink:
    def __init__(self) -> None:
        self.events: list[AnalysisEvent] = []

    def emit(self, event: AnalysisEvent) -> PublishReceipt:
        self.events.append(event)
        return PublishReceipt(accepted=True, reason="captured")


class FailingSink:
    def emit(self, event: AnalysisEvent) -> PublishReceipt:
        raise RuntimeError("sink unavailable")


def test_observability_records_trajectory_and_sanitized_event(tmp_path: Path):
    sink = CapturingSink()
    observer = OrchestratorObservability(
        trajectory_path=tmp_path / "run-1" / "trajectory.jsonl",
        analysis_event_sink=sink,
        task_id="issue-17",
        run_id="run-1",
        evidence_level="REPOSITORY_TESTS",
    )
    reward = RewardBreakdown(
        delta_loc=2,
        delta_cc=1,
        retry_count=1,
        mutation_kill_rate=0.5,
        adversarial_passed=True,
        reward=6.0,
    )

    observer.record_trajectory(
        attempt=2,
        status="JUDGE_SCORED",
        message="Bearer abcdefghijklmnopqrstuvwxyz",
        agent="JUDGE",
        metadata={"graph": "PREPARE -> JUDGE"},
        reward=reward,
    )
    observer.emit_analysis_event(
        AnalysisEventType.PYTEST_FAILED,
        attempt=2,
        phase="pytest",
        error_category="tests_failed",
        recoverable=True,
        safe_metrics={"returncode": 1, "duration_seconds": 0.25},
    )

    trajectory = json.loads(
        (tmp_path / "run-1" / "trajectory.jsonl").read_text(encoding="utf-8").strip()
    )
    assert trajectory["attempt"] == 2
    assert trajectory["status"] == "JUDGE_SCORED"
    assert trajectory["message"] == "Bearer [REDACTED]"
    assert trajectory["metadata"] == {"graph": "PREPARE -> JUDGE"}
    assert trajectory["reward"]["reward"] == 6.0

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_type == AnalysisEventType.PYTEST_FAILED
    assert event.task_id == "issue-17"
    assert event.run_id == "run-1"
    assert event.source == "orchestrator"
    assert event.phase == "pytest"
    assert event.attempt == 2
    assert event.evidence_level == "REPOSITORY_TESTS"
    assert event.error_category == "tests_failed"
    assert event.recoverable is True
    assert event.safe_metrics == {"returncode": 1, "duration_seconds": 0.25}


def test_analysis_event_sink_failure_does_not_escape(tmp_path: Path, caplog):
    observer = OrchestratorObservability(
        trajectory_path=tmp_path / "trajectory.jsonl",
        analysis_event_sink=FailingSink(),
        task_id="run-2",
        run_id="run-2",
        evidence_level="STATIC",
    )

    with caplog.at_level(logging.ERROR):
        observer.emit_analysis_event(
            AnalysisEventType.PHASE_STARTED,
            attempt=0,
            phase="prepare",
        )

    assert "Analysis event emission failed for run run-2" in caplog.text
    assert not (tmp_path / "trajectory.jsonl").exists()
