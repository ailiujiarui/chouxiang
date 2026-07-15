# Reliability, Cross-Repository Benchmark, and Operations Dashboard Design

Date: 2026-07-14
Status: Implemented, verified, self-reviewed, and approved for integration on 2026-07-15

## Objective

Deliver three sequential, independently reviewable phases:

1. production-grade cooperative cancellation, deadlines, lease ownership, and safe retry controls;
2. a reproducible cross-repository benchmark with token, cost, and failure evidence;
3. a read-mostly Streamlit operations dashboard that consumes the reliability and benchmark data.

The implementation builds on the real LangGraph execution workflow, controlled AST rewrite, hardened Docker sandbox, SQLite worker, and PR #3 branch. It does not weaken any existing webhook, Git credential, AST, or Docker boundary.

## Delivery Sequence

The phases are implemented in dependency order:

1. **Reliability control plane** defines job states, events, deadlines, cancellation, and retry APIs.
2. **Benchmark evidence** stores run and case results through the same SQLite boundary.
3. **Operations dashboard** reads those records and invokes only authenticated control APIs.

Each phase receives focused tests and a code-review gate before the next phase starts. Implementation work uses a new branch based on `feat/langgraph-controlled-ast-refactor`; no commit, push, merge, or deployment occurs without a separate approval after implementation review.

## Shared Control Plane

SQLite remains the system of record. Files under the configured run root remain the store for bounded source, diff, and log artifacts.

### Job States

`GitHubJobRecord.status` supports:

- `QUEUED`
- `RUNNING`
- `CANCEL_REQUESTED`
- `CANCELLED`
- `TIMED_OUT`
- `SUCCESS`
- `FAILED`
- `DRY_RUN`

Legal transitions are:

| Source | Targets |
| --- | --- |
| `QUEUED` | `RUNNING`, `CANCELLED` |
| `RUNNING` | `CANCEL_REQUESTED`, `TIMED_OUT`, `SUCCESS`, `FAILED`, `DRY_RUN` |
| `CANCEL_REQUESTED` | `CANCELLED`, `TIMED_OUT`, `FAILED` |
| `FAILED` | `QUEUED` through authenticated manual retry |
| `CANCELLED` | `QUEUED` through authenticated manual retry |
| `TIMED_OUT` | `QUEUED` through authenticated manual retry |

`SUCCESS` and `DRY_RUN` are terminal. Any record with a non-empty PR URL is terminal and cannot be retried.

### Job Events

Add an append-only `job_events` table:

| Column | Type | Meaning |
| --- | --- | --- |
| `event_id` | text primary key | UUID-based event identity |
| `job_id` | text indexed | Parent job |
| `event_type` | text | State transition or control event |
| `from_status` | nullable text | Previous state |
| `to_status` | nullable text | New state |
| `worker_id` | nullable text | Lease owner involved |
| `attempt` | integer | Current automatic attempt |
| `message` | text | Bounded, sanitized reason |
| `created_at` | text | UTC timestamp |

State updates and their event insert execute in one `BEGIN IMMEDIATE` transaction.

### Run Artifacts

For each refactor run, write bounded artifacts under `<run_root>/<run_id>/artifacts`:

- `original.py`
- `candidate.py`
- `change.diff`
- `pytest.log`
- `adversary.log`
- `mutation.json`
- `report.md`

Text logs are UTF-8, redacted using credential-pattern filters, and capped at 256 KiB each. Source and diff artifacts are local-only and are not copied into SQLite, GitHub comments, or benchmark JSON by default.

## Phase 1: Production Reliability

### Execution Control

Add `ExecutionControl` as a runtime dependency with:

- `deadline_at: datetime`
- `remaining_seconds() -> float`
- `cancel_requested() -> bool`
- `checkpoint(stage: str) -> None`

`checkpoint` raises a typed `ExecutionCancelled` or `ExecutionDeadlineExceeded` exception. The LangGraph wrapper calls it before and after every real node. The orchestrator routes either exception to `finalize`, writes terminal trajectory evidence, and does not invoke another node.

The Webhook default deadline is 900 seconds through `REFACTOR_AGENT_JOB_DEADLINE_SECONDS`. Local CLI commands expose `--deadline` with the same default and allow values from 30 to 7200 seconds.

Every sandbox, Git subprocess, and GitHub HTTP operation receives `min(component_timeout, remaining_seconds)` as its timeout. A non-positive remaining time fails before the operation begins.

### Cooperative Cancellation

Cancellation is cooperative:

- queued jobs transition directly to `CANCELLED`;
- running jobs transition to `CANCEL_REQUESTED`;
- the active graph stops at the next node boundary;
- a running pytest, Docker, Git, or HTTP call may finish only until its bounded timeout;
- after the operation returns, the next checkpoint finalizes the task as `CANCELLED`.

GitHub automation checks cancellation before clone, branch creation, candidate application, push, PR creation, and Issue comment creation. Cancellation after push but before PR creation produces `FAILED` with explicit manual-cleanup evidence rather than silently retrying.

### Lease Ownership

Completion, failure, timeout, and cancellation updates require both `job_id` and the current `lease_owner`. A stale worker that lost its lease cannot overwrite a reclaimed job. Heartbeat failure sets a local cancellation flag so the stale worker stops at its next checkpoint.

### Admin APIs

Add authenticated endpoints guarded by the existing Admin Token:

- `POST /jobs/{job_id}/cancel`
- `POST /jobs/{job_id}/retry`
- `GET /jobs/{job_id}/events`

Cancel returns `202` for a newly accepted request, `200` for an already requested cancellation, and `409` for terminal jobs. Retry accepts only `FAILED`, `CANCELLED`, or `TIMED_OUT`, resets the automatic attempt counter, clears lease fields, preserves all events, and returns `409` when a PR URL exists.

## Phase 2: Cross-Repository Benchmark

### Manifest Contract

Create `benchmarks/manifest.toml`, parsed with Python `tomllib`. Each case contains:

- stable case name and category;
- canonical `owner/repo` identity;
- exact 40-character commit SHA;
- target file and pytest path;
- Issue text and expected terminal status;
- seed patch path and gold target-region snapshot;
- allowed import roots;
- Docker test command.

The first manifest pins:

| Repository | Commit |
| --- | --- |
| `more-itertools/more-itertools` | `da37f9de442b69fbcaa9f54fb042c2a6999473a6` |
| `mahmoud/boltons` | `979fa9b613fa8c0a455ae16ea6f2ec91c11ecafe` |
| `grantjenks/python-sortedcontainers` | `3ac358631f58c1347f1d6d2d92784117db0f38ed` |

The eight initial cases are:

| Case | Target | Tests | Seeded defect |
| --- | --- | --- | --- |
| `more-take-off-by-one` | `more_itertools/recipes.py::take` | `tests/test_recipes.py` | consumes one fewer item |
| `more-chunked-strict` | `more_itertools/more.py::chunked` | `tests/test_more.py` | ignores incomplete strict chunk |
| `more-first-default` | `more_itertools/more.py::first` | `tests/test_more.py` | returns marker instead of supplied default |
| `boltons-clamp-bounds` | `boltons/mathutils.py::clamp` | `tests/test_mathutils.py` | inverts lower-bound comparison |
| `boltons-camel-boundary` | `boltons/strutils.py::camel2under` | `tests/test_strutils.py` | mishandles acronym boundary |
| `boltons-chunk-overlap` | `boltons/iterutils.py::chunk_ranges` | `tests/test_iterutils.py` | applies overlap in the wrong direction |
| `sorted-list-contains` | `src/sortedcontainers/sortedlist.py::SortedList.__contains__` | `tests/test_coverage_sortedlist.py` | misses a boundary value |
| `sorted-set-contains` | `src/sortedcontainers/sortedset.py::SortedSet.__contains__` | `tests/test_coverage_sortedset.py` | delegates to the wrong backing collection |

Seed patches and gold snapshots live under `benchmarks/cases/<case-name>/`. The deterministic provider returns the pinned gold target region; DeepSeek receives the seeded repository and Issue text.

### Repository and Container Safety

External benchmark repositories use anonymous canonical GitHub HTTPS clones only. Bare clones are cached under `.benchmark-cache/<owner>/<repo>`, origin is verified, and each run checks out the exact manifest commit into an ephemeral directory.

External cases require Docker. The benchmark image uses Python 3.12 and `pytest==9.1.1`; `benchmarks/requirements.lock` pins hashes for the test toolchain. Repositories install with `python -m pip install --no-deps -e .` inside the container, so repository dependency resolution cannot reach the network. Runtime networking is disabled. Repository installation and tests execute in a container-only writable temporary directory; the host checkout remains read-only. No GitHub, DeepSeek, webhook, or admin credential enters the container.

### Usage and Cost Evidence

Extend LLM results with optional usage metadata:

- prompt tokens;
- completion tokens;
- total tokens;
- provider-reported or locally estimated USD cost;
- provider and model name.

Mock usage is zero. DeepSeek usage comes from the API response. API keys are never persisted or rendered.

Add `benchmark_runs` and `benchmark_case_results` tables. Store manifest hash, repository, commit, provider, model, status, failure category, attempts, LOC/CC, mutation result, adversarial result, runtime, token usage, cost, and normalized result hash.

Failure categories are fixed to:

- `TARGETING`
- `AST_GUARD`
- `PYTEST`
- `ADVERSARY`
- `MUTATION`
- `TIMEOUT`
- `PROVIDER`
- `INFRASTRUCTURE`

### Benchmark CLI

Preserve the current built-in quick benchmark. Add manifest mode:

```powershell
refactor-agent benchmark --manifest benchmarks/manifest.toml --provider mock
refactor-agent benchmark --manifest benchmarks/manifest.toml --provider deepseek
refactor-agent benchmark --compare <previous-run-id>
```

Manifest mode emits JSON and Markdown, writes SQLite evidence, and returns non-zero only for infrastructure failure or when an actual status differs from the manifest expectation.

## Phase 3: Operations Dashboard

### Access Model

Streamlit remains the UI framework and binds to `127.0.0.1` by default. Read views require no Admin Token. Control actions require:

- configured `REFACTOR_AGENT_API_URL`;
- an Admin Token entered through a password input;
- token storage only in Streamlit session state.

The token is never written to URL parameters, logs, SQLite, browser persistent storage, or run artifacts. The Dashboard never updates SQLite directly.

Admin users can submit an allowlisted canonical GitHub URL through `POST /jobs/url`. The Dashboard sends structured input to the API and never invokes Git, the CLI, Docker, or repository code directly. Repository allowlist management is specified by `repository-allowlist-dashboard-design.md`; it is the only Dashboard configuration mutation added to this phase.

### Views

The Dashboard uses Simplified Chinese for fixed user-facing copy and contains four dense, work-focused tabs:

1. **任务**: status, deadline, remaining time, lease owner, attempts, events, cancel, and retry.
2. **执行过程**: actual node timeline, Judge verdict, retry feedback, pytest/adversary/mutation summaries, and bounded logs.
3. **代码变更**: original source, candidate source, unified diff, selected AST targets with reasons, changed regions, and admitted imports.
4. **基准测试**: repository, manifest, provider, model, failure category, tokens, cost, metrics, and two-run comparison.

Known statuses render as `中文（RAW_ENUM）`; control availability continues to use the raw enum. API URLs, identifiers, source, diffs, and diagnostic logs are never translated.

The Tasks tab includes a Chinese URL submission form for repository URL, optional ref, optional target, test path, and refactor request. Repository URL jobs use the same durable queue but are dispatched to `LocalRepositoryRefactorService`, which has no GitHub write API dependency and always returns local `DRY_RUN` evidence on success. It never creates a branch, writes back to the checkout, pushes, opens a PR, or comments on an Issue.

Control buttons follow server state:

- cancel enabled only for `QUEUED` and `RUNNING`;
- retry enabled only for `FAILED`, `CANCELLED`, and `TIMED_OUT` without a PR URL;
- conflicts and authorization failures are displayed explicitly;
- successful controls refresh the selected job and event timeline.

### View Models

Keep Streamlit rendering thin. Add pure functions that convert job, event, trajectory, artifact, and benchmark records into table rows and timeline entries. Artifact loading validates paths under the configured run root and never follows a symlink outside it.

## Error Handling and Security

- Invalid state transitions return `409` and append no event.
- Missing jobs return `404`; invalid Admin authentication returns `401`.
- URL submission authenticates before body parsing, enforces body limits, accepts only canonical GitHub HTTPS URLs, rechecks the repository allowlist in the Worker, and rejects unsafe refs or repository paths.
- Deadline and cancellation terminal results preserve the last completed node and reason.
- Artifact reads reject path traversal and symlink escape.
- Log redaction covers GitHub, DeepSeek, webhook, admin, bearer, and common high-entropy token patterns.
- Benchmark clone, setup, and test failures use bounded error messages and the fixed failure taxonomy.
- A stale worker cannot complete, fail, cancel, or time out a job it no longer owns.

## Testing and Acceptance

### Reliability

- Cover every legal and illegal job transition.
- Prove queued cancellation, running cancellation, node-boundary cancellation, deadline before node, deadline during sandbox, and timeout finalization.
- Prove stale lease owners cannot write terminal state.
- Prove heartbeat loss requests local cancellation.
- Prove manual retry preserves events, resets automatic attempts, and rejects jobs with PR URLs.
- Prove cancellation checks run before every irreversible GitHub side effect.

### Benchmark

- Validate manifest schema, canonical repositories, full SHA pins, paths, expected statuses, and manifest hash stability.
- Run the built-in six-case suite in CI.
- Run one external manifest case in the Docker CI job with mock provider.
- Verify all eight curated cases locally in mock mode.
- Verify normalized results are identical across two mock runs except timestamps and durations.
- Verify DeepSeek usage parsing with mocked API responses; no real provider secret is required in CI.
- Verify external subprocess mode is rejected.

### Dashboard

- Unit-test view models, filters, button availability, diff rendering, artifact path validation, and redaction.
- Test Admin API success, authentication failure, conflict, and stale-state behavior.
- Use Streamlit AppTest for all four tabs and read-only/admin states.
- Prove URL form submission sends the Admin header only on the control request and displays the created Job ID.
- Prove local-only URL jobs cannot enter any GitHub write path even when global dry-run is disabled.
- Launch a local headless Streamlit smoke test and verify HTTP 200.

### Final Gate

- Full unit suite passes.
- Hardened Docker Demo passes.
- External benchmark mock run matches manifest expectations.
- Two benchmark runs match after removing timestamps and durations.
- Dashboard smoke test passes in read-only and Admin-enabled configurations.
- `git diff --check` and credential-pattern scan pass.
- Complete code review finds no unresolved Critical or High issue.

## Out of Scope

- Immediate process or container kill for manual cancellation.
- Dynamic GitHub repository or Issue sampling.
- Dashboard-based configuration editing other than the repository allowlist, arbitrary command execution, repository creation, or benchmark manifest editing.
- Distributed queues, Redis, PostgreSQL, Kubernetes, or multi-host workers.
- Production deployment, PR merge, or external webhook replay without separate approval.

## Implementation Evidence

- Automated unit and integration suite: 233 passed on Python 3.12 after the repository allowlist Dashboard extension review.
- Focused coverage includes state transitions/events, cancellation/retry APIs, lease ownership, graph deadlines, bounded artifacts, manifest/cache validation, benchmark persistence, API view models, and Streamlit AppTest.
- The operations Dashboard uses Simplified Chinese fixed copy, preserves raw identifiers and diagnostic content, and renders known statuses with their original enum values.
- Allowlisted Dashboard URL jobs enter the durable queue and execute through a separate local-only service that has no GitHub write API dependency.
- Eight seed patches were checked against their exact pinned public-repository commits with `git apply --reverse --check` after generation from clean fixed-commit checkouts.
- Real Docker external execution and real DeepSeek execution were not run during implementation. They remain explicit acceptance steps and are not claimed as completed evidence.
- No implementation commit, push, merge, deployment, or credential-bearing acceptance action was performed.
