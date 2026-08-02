from pathlib import Path
from types import SimpleNamespace

from refactor_agent.analysis_events import AnalysisEventType
from refactor_agent.models import (
    EvidenceLevel,
    MetricsSnapshot,
    ReportPersona,
    RewardBreakdown,
)
from refactor_agent.orchestrator_finalize import run_finalize_execution_node
from refactor_agent.orchestrator_state import initial_execution_state


class Store:
    def __init__(self) -> None:
        self.records = []
        self.memories = []

    def save(self, record) -> None:
        self.records.append(record)

    def save_memory(self, record) -> None:
        self.memories.append(record)


def test_finalize_node_persists_and_assembles_success_result(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = _success_state(workspace)
    store = Store()
    reports: list[tuple[tuple, dict]] = []
    artifacts: list[tuple] = []
    events: list[tuple[tuple, dict]] = []

    returned = run_finalize_execution_node(
        state,
        store=store,
        workspace=workspace,
        run_id="run-1",
        issue_id="issue-1",
        repo_name="octo/demo",
        memory_key="module.py",
        evidence_level=EvidenceLevel.REPOSITORY_TESTS,
        report_persona=ReportPersona.TSUNDERE,
        graph_backend="langgraph",
        build_report=lambda *args, **kwargs: reports.append((args, kwargs)) or "# report",
        write_artifacts=lambda *args: artifacts.append(args),
        record_trajectory=lambda *args, **kwargs: None,
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert returned is state
    assert state["next_node"] == "finalize"
    assert len(store.records) == len(store.memories) == 1
    assert state["result"].record == store.records[0]
    assert state["result"].report_markdown == "# report"
    assert state["result"].workspace_path == workspace
    assert state["result"].graph_node_trace == ["PREPARE", "JUDGE", "FINALIZE"]
    assert state["result"].report_persona == ReportPersona.TSUNDERE
    assert reports[0][0][2] == "looks good"
    assert reports[0][0][13] == ["PREPARE", "JUDGE", "FINALIZE"]
    assert artifacts == [(state, "# report")]
    assert events[0][0][0] == AnalysisEventType.FINAL_VERDICT_PASSED
    assert events[0][1]["error_category"] is None
    assert events[0][1]["safe_metrics"]["reward"] == 4.0


def test_finalize_node_records_failure_and_failed_event(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = initial_execution_state(1)
    state.update(
        {
            "attempt": 1,
            "baseline": MetricsSnapshot(loc=8, cyclomatic_complexity=2),
            "previous_error": "tests failed",
            "control_status": "FAILED",
            "debate_rounds": [],
            "node_trace": ["PREPARE"],
            "target_file": workspace / "module.py",
        }
    )
    trajectories: list[tuple] = []
    events: list[tuple[tuple, dict]] = []

    run_finalize_execution_node(
        state,
        store=Store(),
        workspace=workspace,
        run_id="run-2",
        issue_id=None,
        repo_name="octo/demo",
        memory_key="module.py",
        evidence_level=EvidenceLevel.STATIC,
        report_persona=ReportPersona.STRICT,
        graph_backend="loop",
        build_report=lambda *args, **kwargs: "failed report",
        write_artifacts=lambda *args: None,
        record_trajectory=lambda *args, **kwargs: trajectories.append(args),
        emit_analysis_event=lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert state["result"].record.status == "FAILED"
    assert trajectories == [(state, "FAILED", "tests failed")]
    assert events[0][0][0] == AnalysisEventType.FINAL_VERDICT_FAILED
    assert events[0][1]["error_category"] == "failed"
    assert events[0][1]["recoverable"] is False


def _success_state(workspace: Path):
    reward = RewardBreakdown(
        delta_loc=2,
        delta_cc=1,
        retry_count=0,
        mutation_kill_rate=1.0,
        adversarial_passed=True,
        reward=4.0,
    )
    state = initial_execution_state(2)
    state.update(
        {
            "approved": True,
            "attempt": 1,
            "baseline": MetricsSnapshot(loc=8, cyclomatic_complexity=2),
            "post": MetricsSnapshot(loc=6, cyclomatic_complexity=1),
            "llm_result": SimpleNamespace(insult_review="looks good"),
            "reward": reward,
            "debate_rounds": [],
            "llm_usages": [],
            "node_trace": ["PREPARE", "JUDGE"],
            "target_file": workspace / "module.py",
        }
    )
    return state
