from types import SimpleNamespace

from refactor_agent.errors import ErrorCode, public_error_message
from refactor_agent.models import (
    EvidenceLevel,
    MetricsSnapshot,
    ReportPersona,
    RewardBreakdown,
    RunRecord,
    TrajectoryMemoryRecord,
)
from refactor_agent.orchestrator_persistence import persist_run_outcome


class CapturingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, RunRecord | TrajectoryMemoryRecord]] = []

    def save(self, record: RunRecord) -> None:
        self.calls.append(("run", record))

    def save_memory(self, record: TrajectoryMemoryRecord) -> None:
        self.calls.append(("memory", record))


def test_persist_run_outcome_saves_success_record_then_memory():
    store = CapturingStore()
    reward = RewardBreakdown(
        delta_loc=3,
        delta_cc=1,
        retry_count=1,
        mutation_kill_rate=1.0,
        adversarial_passed=True,
        reward=9.0,
    )

    outcome = persist_run_outcome(
        store,
        {
            "approved": True,
            "attempt": 2,
            "baseline": MetricsSnapshot(loc=12, cyclomatic_complexity=3),
            "post": MetricsSnapshot(loc=9, cyclomatic_complexity=2),
            "llm_result": SimpleNamespace(insult_review="smaller and clearer"),
            "reward": reward,
        },
        run_id="run-success",
        issue_id="issue-1",
        repo_name="octo/demo",
        memory_key="module.py",
        evidence_level=EvidenceLevel.REPOSITORY_TESTS,
        report_persona=ReportPersona.TSUNDERE,
    )

    assert [name for name, _ in store.calls] == ["run", "memory"]
    assert outcome.approved is True
    assert outcome.error is None
    assert outcome.attempts == 2
    assert outcome.record == store.calls[0][1]
    assert outcome.record.model_dump() == {
        "run_id": "run-success",
        "issue_id": "issue-1",
        "repo_name": "octo/demo",
        "pre_loc": 12,
        "post_loc": 9,
        "pre_cc": 3,
        "post_cc": 2,
        "self_heal_count": 1,
        "status": "SUCCESS",
        "error": None,
        "error_code": None,
        "error_message": None,
        "error_summary": None,
        "evidence_level": EvidenceLevel.REPOSITORY_TESTS,
        "report_persona": ReportPersona.TSUNDERE,
        "pytest_duration_seconds": None,
        "profiled_pytest_duration_seconds": None,
        "peak_memory_kib": None,
        "import_time_seconds": None,
    }
    memory = store.calls[1][1]
    assert isinstance(memory, TrajectoryMemoryRecord)
    assert memory.run_id == "run-success"
    assert memory.target_path == "module.py"
    assert memory.status == "SUCCESS"
    assert memory.reward == 9.0
    assert "smaller and clearer" in memory.lesson


def test_persist_run_outcome_saves_exhausted_failure_without_post_metrics():
    store = CapturingStore()

    outcome = persist_run_outcome(
        store,
        {
            "attempt": 2,
            "baseline": MetricsSnapshot(loc=12, cyclomatic_complexity=3),
            "post": MetricsSnapshot(loc=9, cyclomatic_complexity=2),
            "previous_error": "AssertionError: expected False",
        },
        run_id="run-failed",
        issue_id=None,
        repo_name="octo/demo",
        memory_key="module.py",
        evidence_level=EvidenceLevel.USER_TESTS,
        report_persona=ReportPersona.STRICT,
    )

    assert [name for name, _ in store.calls] == ["run", "memory"]
    assert outcome.approved is False
    assert outcome.error == "AssertionError: expected False"
    assert outcome.attempts == 2
    assert outcome.record.status == "FAILED"
    assert outcome.record.self_heal_count == 2
    assert outcome.record.post_loc is None
    assert outcome.record.post_cc is None
    memory = store.calls[1][1]
    assert isinstance(memory, TrajectoryMemoryRecord)
    assert memory.status == "FAILED"
    assert memory.run_id == "run-failed"


def test_persist_run_outcome_preserves_sanitized_terminal_error_fields():
    store = CapturingStore()
    message = public_error_message(ErrorCode.INTERNAL_ERROR)

    outcome = persist_run_outcome(
        store,
        {
            "attempt": 1,
            "terminal_error": message,
            "terminal_error_code": ErrorCode.INTERNAL_ERROR,
            "terminal_error_summary": "provider unavailable",
        },
        run_id="run-terminal",
        issue_id=None,
        repo_name="octo/demo",
        memory_key="module.py",
        evidence_level=EvidenceLevel.STATIC,
        report_persona=ReportPersona.STRICT,
    )

    assert outcome.record.self_heal_count == 0
    assert outcome.record.error is None
    assert outcome.record.error_code == ErrorCode.INTERNAL_ERROR
    assert outcome.record.error_message == message
    assert outcome.record.error_summary == "provider unavailable"
    memory = store.calls[1][1]
    assert isinstance(memory, TrajectoryMemoryRecord)
    assert memory.error_signature == ErrorCode.INTERNAL_ERROR.value
