# Execution Graph, AST Targeting, and Evidence Remediation Design

Date: 2026-07-13
Status: Implemented and self-reviewed (2026-07-13)

## Objective

Replace the decorative LangGraph decision recorder with a workflow that executes the actual refactor loop, make AST target selection follow Issue and failure evidence instead of complexity alone, and add reproducible CI evidence. Preserve the completed webhook, credential, Docker, and durable-worker security boundaries.

## Real LangGraph Execution

### Workflow Shape

The production graph will execute these nodes:

1. `prepare`: create the isolated workspace, load trajectory memory, analyze the baseline, and resolve allowed AST regions.
2. `minimizer`: call the LLM and produce an untrusted full-file candidate.
3. `ast_guard`: compute the independent AST diff, perform controlled region replacement, and reject unsafe candidates before execution.
4. `pytest`: write the controlled candidate and run baseline tests in the selected sandbox.
5. `adversary`: generate and execute boundary tests.
6. `mutation`: run mutation tests and performance profiling.
7. `judge`: calculate reward and return `APPROVE`, `RETRY`, or `REJECT`.
8. `finalize`: persist the run, trajectory memory, report, and terminal result.

Conditional edges own all retry behavior:

- `ast_guard -> minimizer` on static rejection when attempts remain.
- `pytest -> minimizer` on regression failure when attempts remain.
- `adversary -> minimizer` on a discovered counterexample when attempts remain.
- `judge -> minimizer` on `RETRY`.
- Any retry edge becomes `finalize` with `FAILED` when `max_retry` is exhausted.
- `judge -> finalize` on `APPROVE` or terminal `REJECT`.

### Shared State and Runtime

- Add `RefactorGraphState`, containing request data, attempt counters, original/current candidate source, selected regions, validation outputs, sandbox outputs, reward, debate messages, error feedback, trajectory steps, and terminal result fields.
- Keep non-serializable dependencies in a `RefactorWorkflowRuntime`: Agents, store, sandbox configuration, workspace paths, and trajectory writer.
- Node functions are methods on one workflow object and are the only place where Agent, filesystem, sandbox, and persistence side effects occur.
- Compile the LangGraph once per workflow instance, not once per node decision.
- Remove `_decide_graph` and the precomputed-boolean graph API.

### Loop Compatibility

- Retain `graph_backend=loop` only as a deterministic executor for debugging and compatibility.
- The loop backend invokes the same node methods and routing functions as LangGraph; it must not duplicate business logic.
- Both backends must produce equivalent terminal records, reports, trajectory statuses, and Agent message order for identical deterministic inputs.

## Issue-Aware AST Targeting

### Target Scoring

Change `select_target_regions` to accept `source`, `issue_text`, and optional `failure_feedback`. Score every top-level function and class method using this precedence:

- Exact qualified symbol named in Issue or traceback: highest priority.
- Function/class name token match in Issue: high priority.
- Traceback line contained by the function or method: high priority.
- Path or line directive pointing at the region: high priority.
- Previous validation or pytest failure mentioning the symbol: medium priority.
- Cyclomatic complexity and node count: fallback ranking only.

Select at most three regions. If evidence identifies one exact symbol, do not add unrelated high-complexity regions merely to fill the limit.

### Module-Level Changes

- Add a `module:<lineno>:<node-type>` target representation for a specifically referenced top-level assignment or control statement.
- Module targets may replace only the matched statement range; unrelated imports, assignments, classes, and functions remain unchanged.
- If no function or module evidence exists, fall back to the highest-complexity function. Files without functions require an explicit line/symbol directive instead of guessing.

### Controlled Imports

- New imports remain forbidden by default.
- Add `allowed_import_roots` to `RefactorRequest` and a CLI repeatable option `--allow-import`.
- Webhook mode reads allowed import roots only from `REFACTOR_AGENT_ALLOWED_IMPORTS`; Issue text cannot grant import permission.
- Permit additions only when every imported root is allowlisted, imports are absolute, no wildcard import is used, and existing imports are not removed or rewritten.
- Continue blocking dangerous modules and calls regardless of allowlist.
- Record every accepted import addition in the report and trajectory.

## CI and Evidence

### Continuous Integration

Add `.github/workflows/ci.yml` with no external secrets:

- Python 3.11 and 3.12 unit-test matrix using mock LLM and subprocess tests.
- A Docker job that builds `docker/sandbox.Dockerfile` and runs one mock Docker demo.
- `git diff --check`, package installation, CLI help, and fail-closed configuration tests.
- No DeepSeek or GitHub write operations in CI.

### Reproducible Benchmark

- Add a `benchmark` CLI command that runs the built-in deterministic cases and emits JSON plus Markdown.
- Record case name, status, attempts, LOC/CC before and after, mutation kill rate, adversarial result, runtime, and reward.
- Include at least six cases covering simple functions, low-complexity bug targets, class methods, module-level statements, adversarial weak tests, and a deliberately rejected unsafe candidate.
- Aggregate claims must include sample count and may only be copied into documentation from generated benchmark output.
- Remove unsupported “55% average” and “100% pass rate” claims until the generated benchmark demonstrates them.

## Interface Changes

- `select_target_regions(source, issue_text, failure_feedback=None, max_regions=3)` returns structured target regions with score and reason.
- `RefactorRequest.allowed_import_roots: set[str]` defaults to empty.
- `RefactorRunResult` exposes the final graph node trace and backend.
- The old `run_debate_graph` precomputed-evidence interface is removed.
- `state-machine` renders the actual compiled graph topology.
- Reports identify why each AST target was selected and which imports, if any, were admitted.

## Tests and Acceptance

### Graph Tests

- Assert actual Agent and sandbox mocks execute from graph nodes in order.
- Cover AST rejection, pytest failure, adversarial failure, judge retry, retry exhaustion, LLM error, Docker unavailability, and success.
- Assert LangGraph and loop backends are behaviorally equivalent.
- Assert trajectory statuses and report messages come from executed nodes, not a separately maintained display graph.

### AST Tests

- Select a low-complexity function explicitly named by an Issue over an unrelated complex function.
- Select class methods and traceback-containing regions.
- Replace an explicitly targeted module-level statement while preserving all other module text.
- Reject unrequested module changes and non-target function changes.
- Accept allowlisted imports and reject wildcard, relative, non-allowlisted, removed, or dangerous imports.
- Preserve comments and target-external source text after every rewrite.

### Final Gate

- Full unit suite passes on local Python.
- Docker-backed demo passes with the hardened image.
- CI workflow syntax is valid and all commands run locally where feasible.
- Benchmark output is reproducible in two consecutive mock runs except for timestamps/runtime fields.
- Perform a full code review after implementation and self-fix all Critical/High findings.
- Do not commit, push, merge PR #3, or deploy without separate user approval after review.

## Documentation Updates

- Update `README.md`, `plan.md`, `plan2.md`, and `security-remediation.md` after implementation.
- Clearly separate completed behavior, deferred behavior, local trusted subprocess mode, and production Docker webhook mode.
- Treat generated benchmark artifacts as evidence; do not present aspirational roadmap numbers as measured results.

## Out of Scope

- No new LLM provider.
- No production deployment.
- No merge or push.
- No changes to Codex/OpenAI authentication files.
- No external webhook replay, which the user explicitly waived.

## Implementation Record

- Real side effects execute in `_RefactorWorkflow` node methods through `run_execution_graph`; LangGraph and loop share the same node methods and legal routing table.
- The obsolete precomputed `run_debate_graph` API and legacy orchestrator loop were removed.
- `RefactorGraphState` records graph control fields while non-serializable Agents, stores, paths, and sandbox settings remain on the workflow runtime object.
- AST selection now prioritizes Issue symbols and traceback/path lines, supports `module:<line>:<type>` targets, and records selected target reasons and admitted imports in trajectory/report evidence.
- `.github/workflows/ci.yml` contains Python 3.11/3.12 unit jobs and a hardened Docker demo job without external secrets.
- The six-case deterministic benchmark produced 5 safe successes and 1 expected unsafe rejection in two consecutive runs; outputs matched after excluding timestamp and runtime fields.
- Final local gate: `111 passed`, one existing Starlette/httpx deprecation warning; CI YAML parsed locally; hardened Docker Demo exited successfully.
