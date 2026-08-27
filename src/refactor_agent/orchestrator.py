from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from refactor_agent.analysis_events import AnalysisEventSink, AnalysisEventType
from refactor_agent.agents import AdversaryAgent, DefenderAgent, JudgeAgent, MinimizerAgent
from refactor_agent.execution_graph import ExecutionState, run_execution_graph
from refactor_agent.execution_control import ExecutionControl
from refactor_agent.llm import RefactorClient
from refactor_agent.memory import target_memory_key
from refactor_agent.models import (
    RefactorRequest,
    RefactorRunResult,
    RewardBreakdown,
)
from refactor_agent.orchestrator_artifacts import write_run_artifacts
from refactor_agent.orchestrator_adversary import run_adversary_execution_node
from refactor_agent.orchestrator_ast_guard import guard_ast_execution_node
from refactor_agent.orchestrator_observability import OrchestratorObservability
from refactor_agent.orchestrator_finalize import run_finalize_execution_node
from refactor_agent.orchestrator_report import build_report as render_report
from refactor_agent.orchestrator_minimizer import minimize_execution_node
from refactor_agent.orchestrator_judge import run_judge_execution_node
from refactor_agent.orchestrator_mutation import run_mutation_execution_node
from refactor_agent.orchestrator_prepare import prepare_execution_node
from refactor_agent.orchestrator_pytest import run_pytest_execution_node
from refactor_agent.orchestrator_state import initial_execution_state
from refactor_agent.store import SQLiteRunStore


class RefactorOrchestrator:
    def __init__(
        self,
        llm_client: RefactorClient,
        run_root: Path = Path(".runs"),
        store: SQLiteRunStore | None = None,
        pytest_timeout_seconds: float = 30.0,
        sandbox_backend: str = "subprocess",
        sandbox_docker_image: str = "refactor-agent-sandbox:py312",
        sandbox_memory: str = "256m",
        sandbox_cpus: float = 1.0,
        graph_backend: str = "langgraph",
        analysis_event_sink: AnalysisEventSink | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.run_root = run_root.resolve()
        self.store = store or SQLiteRunStore(self.run_root / "refactor_agent.sqlite")
        self.analysis_event_sink = analysis_event_sink or self.store
        self.pytest_timeout_seconds = pytest_timeout_seconds
        self.sandbox_backend = sandbox_backend
        self.sandbox_docker_image = sandbox_docker_image
        self.sandbox_memory = sandbox_memory
        self.sandbox_cpus = sandbox_cpus
        if graph_backend not in {"langgraph", "loop"}:
            raise ValueError(f"Unsupported graph backend: {graph_backend}")
        self.graph_backend = graph_backend
        self.minimizer = MinimizerAgent(llm_client)
        self.defender = DefenderAgent()
        self.adversary = AdversaryAgent()
        self.judge = JudgeAgent()

    def run(
        self,
        request: RefactorRequest,
        execution_control: ExecutionControl | None = None,
    ) -> RefactorRunResult:
        control = execution_control or ExecutionControl(
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=900)
        )
        return _RefactorWorkflow(self, request, control).run()


class _RefactorWorkflow:
    def __init__(
        self,
        orchestrator: RefactorOrchestrator,
        request: RefactorRequest,
        execution_control: ExecutionControl,
    ) -> None:
        self.orchestrator = orchestrator
        self.request = request
        self.run_id = _new_run_id()
        self.workspace = orchestrator.run_root / self.run_id / "workspace"
        self.repo_name = request.repo_name or request.target_file.resolve().parent.name
        self.memory_key = target_memory_key(request.target_file)
        self.trajectory_path = orchestrator.run_root / self.run_id / "trajectory.jsonl"
        self.observability = OrchestratorObservability(
            trajectory_path=self.trajectory_path,
            analysis_event_sink=orchestrator.analysis_event_sink,
            task_id=request.issue_id or self.run_id,
            run_id=self.run_id,
            evidence_level=request.evidence_level.value,
        )
        self.execution_control = execution_control

    def run(self) -> RefactorRunResult:
        final = run_execution_graph(
            initial_execution_state(self.request.max_retry),
            self,
            self.orchestrator.graph_backend,
            execution_control=self.execution_control,
        )
        result: RefactorRunResult = final["result"]
        result.graph_backend = self.orchestrator.graph_backend
        result.graph_node_trace = final["node_trace"]
        return result

    def prepare(self, state: ExecutionState) -> ExecutionState:
        """Prepare the run and convert sandbox startup details to a sanitized terminal error."""

        self._phase_started(state, "prepare")
        return prepare_execution_node(
            state,
            request=self.request,
            workspace=self.workspace,
            store=self.orchestrator.store,
            repo_name=self.repo_name,
            memory_key=self.memory_key,
            sandbox_backend=self.orchestrator.sandbox_backend,
        )

    def minimizer(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "minimizer")
        return minimize_execution_node(
            state,
            request=self.request,
            minimizer=self.orchestrator.minimizer,
            record_trajectory=self._trajectory,
        )

    def ast_guard(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "ast_guard")
        return guard_ast_execution_node(
            state,
            allowed_import_roots=self.request.allowed_import_roots,
            defender=self.orchestrator.defender,
            emit_analysis_event=self._emit_analysis_event,
            record_trajectory=self._trajectory,
        )

    def pytest(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "pytest")
        return run_pytest_execution_node(
            state,
            workspace=self.workspace,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            docker_image=self.orchestrator.sandbox_docker_image,
            docker_memory=self.orchestrator.sandbox_memory,
            docker_cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
            defender=self.orchestrator.defender,
            emit_analysis_event=self._emit_analysis_event,
            record_trajectory=self._trajectory,
        )

    def adversary(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "adversary")
        return run_adversary_execution_node(
            state,
            issue_text=self.request.issue_text,
            workspace=self.workspace,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            docker_image=self.orchestrator.sandbox_docker_image,
            docker_memory=self.orchestrator.sandbox_memory,
            docker_cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
            adversary=self.orchestrator.adversary,
            emit_analysis_event=self._emit_analysis_event,
            record_trajectory=self._trajectory,
        )

    def mutation(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "mutation")
        return run_mutation_execution_node(
            state,
            workspace=self.workspace,
            timeout_seconds=self.orchestrator.pytest_timeout_seconds,
            docker_image=self.orchestrator.sandbox_docker_image,
            docker_memory=self.orchestrator.sandbox_memory,
            docker_cpus=self.orchestrator.sandbox_cpus,
            execution_control=self.execution_control,
            adversary=self.orchestrator.adversary,
            record_trajectory=self._trajectory,
        )

    def judge(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "judge")
        return run_judge_execution_node(
            state,
            graph_backend=self.orchestrator.graph_backend,
            judge=self.orchestrator.judge,
            record_trajectory=self._trajectory,
        )

    def finalize(self, state: ExecutionState) -> ExecutionState:
        self._phase_started(state, "finalize")
        return run_finalize_execution_node(
            state,
            store=self.orchestrator.store,
            workspace=self.workspace,
            run_id=self.run_id,
            issue_id=self.request.issue_id,
            repo_name=self.repo_name,
            memory_key=self.memory_key,
            evidence_level=self.request.evidence_level,
            report_persona=self.request.persona,
            graph_backend=self.orchestrator.graph_backend,
            build_report=render_report,
            write_artifacts=self._write_artifacts,
            record_trajectory=self._trajectory,
            emit_analysis_event=self._emit_analysis_event,
        )

    def _write_artifacts(self, state: ExecutionState, report: str) -> None:
        write_run_artifacts(self.orchestrator.run_root, self.run_id, state, report)

    def _trajectory(
        self,
        state: ExecutionState,
        status: str,
        message: str,
        agent: str | None = None,
        metadata: dict | None = None,
        reward: RewardBreakdown | None = None,
    ) -> None:
        self.observability.record_trajectory(
            attempt=int(state.get("attempt", 0)),
            status=status,
            message=message,
            agent=agent,
            metadata=metadata,
            reward=reward,
        )

    def _emit_analysis_event(
        self,
        event_type: AnalysisEventType,
        state: ExecutionState,
        *,
        phase: str | None = None,
        error_category: str | None = None,
        recoverable: bool | None = None,
        safe_metrics: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        self.observability.emit_analysis_event(
            event_type,
            attempt=int(state.get("attempt", 0)),
            phase=phase,
            error_category=error_category,
            recoverable=recoverable,
            safe_metrics=safe_metrics,
        )

    def _phase_started(self, state: ExecutionState, phase: str) -> None:
        self._emit_analysis_event(
            AnalysisEventType.PHASE_STARTED,
            state,
            phase=phase,
        )


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"
