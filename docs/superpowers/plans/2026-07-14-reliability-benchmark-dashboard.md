# Reliability, Benchmark, and Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable job cancellation and deadlines, reproducible cross-repository benchmark evidence, and a read-mostly four-tab Streamlit operations dashboard.

**Architecture:** SQLite remains the transactional control plane for jobs, events, runs, and benchmarks. Runtime cancellation flows through a small `ExecutionControl` dependency used by the graph, sandbox, worker, and GitHub service; benchmark execution is isolated behind a manifest/repository/Docker boundary; Streamlit consumes pure view models and authenticated HTTP control APIs.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, LangGraph, FastAPI, Typer, Docker SDK/CLI, Streamlit, pytest 9.1.1.

## Global Constraints

- Preserve `plan.md` and `plan2.md`; do not modify, stage, commit, or delete them.
- Use test-driven development: every production behavior starts with a focused failing test.
- Webhook jobs default to a 900-second deadline; CLI accepts 30 through 7200 seconds.
- External benchmarks require Docker, Python 3.12, `pytest==9.1.1`, anonymous canonical GitHub clones, pinned commits, and disabled runtime networking.
- Never persist or render GitHub, DeepSeek, webhook, bearer, or Admin credentials.
- Do not commit, push, merge, deploy, or perform real provider/GitHub acceptance during implementation.
- Complete a full code review and self-fix all Critical and High findings before asking for integration approval.

---

### Task 1: Transactional Job State and Event Ledger

**Files:**
- Modify: `src/refactor_agent/models.py`
- Modify: `src/refactor_agent/store.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Produces: `GitHubJobStatus`, `JobEventRecord`, `JobTransitionError`.
- Produces: `transition_github_job(job_id, to_status, *, worker_id=None, message="", require_owner=False)` and `list_job_events(job_id)`.

- [ ] Add tests for every legal transition, representative illegal transitions, append-only event ordering, and event/state atomicity.
- [ ] Run `pytest tests/test_store.py -q` and confirm failures reference missing statuses/events/transition APIs.
- [ ] Add the status enum, event model, schema migration, legal transition table, and a `BEGIN IMMEDIATE` transactional transition method.
- [ ] Require lease ownership for worker terminal transitions and reject stale owners without changing state or appending events.
- [ ] Run `pytest tests/test_store.py -q` and confirm all store tests pass.

### Task 2: Execution Control, Deadlines, and Graph Checkpoints

**Files:**
- Create: `src/refactor_agent/execution_control.py`
- Modify: `src/refactor_agent/execution_graph.py`
- Modify: `src/refactor_agent/orchestrator.py`
- Modify: `src/refactor_agent/config.py`
- Modify: `src/refactor_agent/cli.py`
- Create: `tests/test_execution_control.py`
- Modify: `tests/test_execution_graph.py`
- Modify: `tests/test_cli_config.py`

**Interfaces:**
- Produces: `ExecutionControl(deadline_at, is_cancel_requested)`, `remaining_seconds()`, `bounded_timeout()`, and `checkpoint(stage)`.
- Produces: typed `ExecutionCancelled` and `ExecutionDeadlineExceeded` exceptions.
- Consumes: graph node names and orchestrator node boundaries.

- [ ] Add tests for pre-node cancellation, post-node cancellation, expired deadline, bounded component timeout, and CLI deadline validation.
- [ ] Run focused tests and confirm they fail because the control type and graph integration do not exist.
- [ ] Implement the clock-injectable execution control and add before/after checkpoints to every real graph and loop node.
- [ ] Route cancellation/deadline exceptions to final trajectory records without executing subsequent nodes.
- [ ] Add `REFACTOR_AGENT_JOB_DEADLINE_SECONDS` and `--deadline` with range validation.
- [ ] Run `pytest tests/test_execution_control.py tests/test_execution_graph.py tests/test_cli_config.py tests/test_orchestrator.py -q`.

### Task 3: Worker Cancellation, Retry, Lease Ownership, and Admin APIs

**Files:**
- Modify: `src/refactor_agent/job_worker.py`
- Modify: `src/refactor_agent/webhook.py`
- Modify: `src/refactor_agent/store.py`
- Modify: `tests/test_webhook.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Consumes: Task 1 transitions/events and Task 2 `ExecutionControl`.
- Produces: `POST /jobs/{job_id}/cancel`, `POST /jobs/{job_id}/retry`, and `GET /jobs/{job_id}/events`.

- [ ] Add tests for queued/running cancellation, idempotent cancellation, terminal conflicts, authenticated retry, PR retry rejection, event preservation, and stale worker completion.
- [ ] Run focused tests and verify expected 404/401/409/202 failures.
- [ ] Wire worker-local cancellation to store status and heartbeat lease loss.
- [ ] Implement cancel/retry store operations as transactions and expose Admin Token guarded endpoints.
- [ ] Ensure completion/failure/cancel/timeout writes include the active lease owner.
- [ ] Run `pytest tests/test_store.py tests/test_webhook.py -q`.

### Task 4: GitHub Side-Effect Checkpoints and Bounded Operations

**Files:**
- Modify: `src/refactor_agent/github.py`
- Modify: `src/refactor_agent/sandbox.py`
- Modify: `tests/test_github.py`
- Modify: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `ExecutionControl.checkpoint()` and `bounded_timeout()`.
- Produces: explicit failed/manual-cleanup result when cancellation follows push but precedes PR creation.

- [ ] Add tests proving checkpoints precede clone, branch, write, push, PR, and Issue comment side effects.
- [ ] Add tests proving subprocess, Docker, and HTTP timeouts never exceed remaining deadline.
- [ ] Run focused tests and confirm missing control arguments cause failure.
- [ ] Thread execution control through GitHub automation and sandbox APIs with backward-compatible defaults for trusted local calls.
- [ ] Track whether push occurred and emit bounded manual-cleanup evidence on later cancellation.
- [ ] Run `pytest tests/test_github.py tests/test_sandbox.py -q`.

### Task 5: Bounded, Redacted Run Artifacts

**Files:**
- Create: `src/refactor_agent/artifacts.py`
- Modify: `src/refactor_agent/orchestrator.py`
- Create: `tests/test_artifacts.py`
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Produces: `RunArtifactWriter(run_dir, max_log_bytes=262144)` and safe artifact path resolution.
- Produces: credential redaction shared by logs, events, and dashboard reads.

- [ ] Add tests for all seven artifact names, UTF-8 truncation, token redaction, traversal rejection, and symlink escape rejection.
- [ ] Run artifact tests and confirm the module is missing.
- [ ] Implement atomic bounded writes, unified diff generation, sanitization, and safe path resolution.
- [ ] Emit original/candidate/diff/test/adversary/mutation/report artifacts from the real run lifecycle.
- [ ] Run `pytest tests/test_artifacts.py tests/test_orchestrator.py -q`.

### Task 6: LLM Usage Metadata

**Files:**
- Modify: `src/refactor_agent/models.py`
- Modify: `src/refactor_agent/llm.py`
- Modify: `tests/test_llm.py`

**Interfaces:**
- Produces: optional `LLMUsage` with provider, model, prompt/completion/total tokens, and USD cost.
- Produces: zero-valued mock usage and parsed DeepSeek usage without exposing API keys.

- [ ] Add tests for mock usage, DeepSeek response parsing, absent usage fields, and sanitized provider errors.
- [ ] Run `pytest tests/test_llm.py -q` and confirm missing usage assertions fail.
- [ ] Extend candidate results with usage while preserving existing provider call signatures.
- [ ] Parse provider token/cost fields and calculate only documented local estimates.
- [ ] Run `pytest tests/test_llm.py tests/test_orchestrator.py -q`.

### Task 7: Benchmark Manifest and Repository Cache

**Files:**
- Create: `src/refactor_agent/benchmark_manifest.py`
- Create: `src/refactor_agent/benchmark_repository.py`
- Create: `benchmarks/manifest.toml`
- Create: `benchmarks/requirements.lock`
- Create: `benchmarks/cases/*/seed.patch`
- Create: `benchmarks/cases/*/gold.py`
- Create: `tests/test_benchmark_manifest.py`
- Create: `tests/test_benchmark_repository.py`

**Interfaces:**
- Produces: `BenchmarkManifest`, `BenchmarkCase`, `load_manifest(path)`, and stable `manifest_hash`.
- Produces: `BenchmarkRepositoryCache.prepare(case, destination)` using canonical anonymous GitHub origins and exact SHAs.

- [ ] Add schema tests for all eight case names, canonical repositories, 40-character SHAs, relative paths, fixed expected statuses, and hash stability.
- [ ] Add repository tests using local bare fixtures for origin validation, exact checkout, cache reuse, and destination isolation.
- [ ] Run the focused tests and confirm missing parser/cache failures.
- [ ] Implement strict `tomllib` parsing and immutable validated models.
- [ ] Implement anonymous bare-cache fetch/checkout without credential-bearing URLs.
- [ ] Add the eight approved case fixtures and run focused tests to green.

### Task 8: Docker-Only External Benchmark Runner

**Files:**
- Create: `src/refactor_agent/benchmark_runner.py`
- Create: `docker/Dockerfile.benchmark`
- Modify: `src/refactor_agent/sandbox.py`
- Create: `tests/test_benchmark_runner.py`

**Interfaces:**
- Consumes: manifest cases, repository cache, orchestrator, and Docker sandbox.
- Produces: normalized `BenchmarkCaseResult` and fixed failure categories.

- [ ] Add tests rejecting subprocess mode and asserting Python 3.12, no network, read-only host checkout, writable container tempdir, no credentials, and bounded timeout settings.
- [ ] Run focused tests and confirm runner/image contract failures.
- [ ] Implement setup, seed application, deterministic gold provider, Docker execution, and failure classification.
- [ ] Ensure repository installation uses `pip install --no-deps -e .` and test toolchain uses the lock file.
- [ ] Run `pytest tests/test_benchmark_runner.py tests/test_sandbox.py -q`.

### Task 9: Benchmark Persistence, Reports, Compare, and CLI

**Files:**
- Modify: `src/refactor_agent/store.py`
- Modify: `src/refactor_agent/benchmark.py`
- Modify: `src/refactor_agent/cli.py`
- Modify: `tests/test_benchmark.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Produces: `benchmark_runs` and `benchmark_case_results` records.
- Produces: manifest mode, provider selection, and `--compare RUN_ID` CLI paths.

- [ ] Add tests for benchmark round trips, normalized hashes, status mismatch exit code, infrastructure exit code, JSON/Markdown fields, and two-run comparison.
- [ ] Run focused tests and confirm persistence and CLI options are absent.
- [ ] Implement transactional benchmark persistence and deterministic normalization excluding timestamps/durations.
- [ ] Extend CLI while preserving the built-in quick benchmark default.
- [ ] Run `pytest tests/test_benchmark.py tests/test_store.py tests/test_cli_config.py -q`.

### Task 10: Dashboard API Client and Pure View Models

**Files:**
- Create: `src/refactor_agent/dashboard_api.py`
- Create: `src/refactor_agent/dashboard_views.py`
- Modify: `src/refactor_agent/dashboard.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Produces: read APIs for jobs/events/runs/artifacts/benchmarks and Admin Token cancel/retry calls.
- Produces: pure task rows, event timelines, execution summaries, code views, benchmark rows, and action availability.

- [ ] Add tests for API auth headers, no token persistence, conflict rendering, filters, button availability, timeline conversion, diff rendering, and safe artifact loading.
- [ ] Run `pytest tests/test_dashboard.py -q` and confirm missing client/view-model failures.
- [ ] Implement a timeout-bounded `httpx` client and pure dataclass-based view models.
- [ ] Keep SQLite access confined to server read endpoints; the dashboard never writes the database.
- [ ] Run `pytest tests/test_dashboard.py tests/test_webhook.py -q`.

### Task 11: Four-Tab Streamlit Operations UI

**Files:**
- Modify: `src/refactor_agent/dashboard.py`
- Modify: `src/refactor_agent/cli.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 10 API client/view models.
- Produces: Tasks, Execution, Code, and Benchmarks tabs with read-only and Admin-enabled states.

- [ ] Add Streamlit AppTest coverage for all four tabs, empty/error states, password token session storage, and enabled/disabled control buttons.
- [ ] Run dashboard tests and confirm tab/control assertions fail.
- [ ] Implement a dense, work-focused UI with no direct database mutations and no token in URL/query/log output.
- [ ] Add local bind/API URL configuration and explicit authorization/conflict messages.
- [ ] Run `pytest tests/test_dashboard.py -q` and launch a headless smoke server that returns HTTP 200.

### Task 12: Documentation, CI Contracts, and Full Review

**Files:**
- Modify: `README.md`
- Modify: `docker/README.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `phase4-reliability-benchmark-dashboard-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-reliability-benchmark-dashboard.md`
- Modify: `tests/test_ci_contract.py`

**Interfaces:**
- Produces: reproducible local commands and CI contract for unit, built-in benchmark, Docker demo, one mock external case, and dashboard smoke.

- [ ] Add failing CI contract tests for the required jobs and credential-free benchmark invocation.
- [ ] Run `pytest tests/test_ci_contract.py -q` and confirm expected workflow assertions fail.
- [ ] Update workflow and documentation with deadlines, cancellation/retry, benchmark evidence, Docker prerequisites, dashboard access, and security boundaries.
- [ ] Mark implemented design sections and record any evidence limitations without claiming real-provider validation.
- [ ] Run `pytest -q`, the built-in benchmark, Docker checks available locally, and dashboard smoke.
- [ ] Run `git diff --check` and scan tracked diffs for credential patterns and forbidden `plan.md`, `plan2.md`, `.runs`, or cache artifacts.
- [ ] Review every changed file for state-machine races, stale leases, timeout gaps, path escapes, credential disclosure, benchmark nondeterminism, UI authorization, and documentation accuracy.
- [ ] Add a failing regression test for each discovered defect, self-fix it, and rerun all relevant focused tests plus `pytest -q`.
- [ ] Update this checklist with final verification evidence; leave all implementation changes uncommitted and unpushed for user approval.

## Execution Record

Implementation and self-review completed on 2026-07-14 in the isolated `feat/reliability-benchmark-dashboard` worktree.

- Reliability: transactional state/events, cancellation, retry, deadlines, lease recovery, ownership checks, side-effect checkpoints, and bounded redacted artifacts implemented.
- Benchmark: eight-case pinned manifest, fixture-content hash, anonymous bare cache, Docker-only runner, usage/cost evidence, SQLite persistence, reports, compare mode, and CI contract implemented.
- Dashboard: read-only FastAPI data endpoints, Admin-only controls, safe artifact reads, pure view models, and four Streamlit operations tabs implemented.
- Review fixes: cancellation lease recovery, old-schema foreign key migration order, persistent/trajectory redaction, PR URL retention after cancellation, fixture symlink rejection, anonymous Git credential isolation, deadline race handling, and mandatory lease ownership.
- Verification before this record: 176 pytest tests passed, built-in benchmark produced five successful repair cases plus the expected unsafe rejection, Streamlit health returned HTTP 200, `compileall` passed, `git diff --check` passed, and credential-pattern scan was clean.
- Not executed: real DeepSeek, real GitHub side effects, and real external Docker benchmark acceptance. No implementation commit or push was performed.
