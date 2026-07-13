# Security Remediation and Repository Cleanup Design

Date: 2026-07-13
Status: Implemented locally; replacement GitHub token authenticated; external private-repository replay waived by user

## Implementation Result (2026-07-13)

- Closed PR #2, deleted its E2E branch, and closed Issue #1. PR #3 remains open and unmerged.
- Deleted historical `.runs`, `.github-workspaces`, and `.github-url-workspaces`; later validation created only a new local run directory without Git credentials.
- Added fail-closed startup validation for webhook secret, admin token, repository allowlist, sender allowlist, Docker backend, Docker availability, GitHub token, and DeepSeek key as applicable.
- Removed webhook-controlled clone URLs and URL-embedded credentials. Git now uses a canonical GitHub URL plus an ephemeral AskPass environment, verifies `origin`, and deletes checkouts by default.
- Added signed delivery idempotency, one active job per repository/Issue, SQLite leases, lease recovery, heartbeat renewal, and a persistent worker loop.
- Hardened Docker with non-root execution, read-only filesystem and repository mount, dropped capabilities, `no-new-privileges`, PID/CPU/memory limits, disabled network, and tmpfs-only writable runtime paths.
- Added admin authentication to job APIs, sender authorization, payload limits, malformed-input handling, symlink escape prevention, deterministic retry branch names, and credential-free test environments.
- Validation: `92 passed`; hardened Docker image built and the Docker-backed leap-year demo completed successfully.
- Replacement token validation completed on 2026-07-13 for GitHub user `ailiujiarui` with private-repository write access. Final signed replay still requires a disposable private repository; the token intentionally lacks `delete_repo`, so repository creation/deletion remains a user-controlled action.
- The user explicitly waived the disposable private-repository webhook replay on 2026-07-13. No validation repository, Issue, branch, Pull Request, or webhook service was created for this final step.

## Objective

Eliminate the current webhook-to-host-code-execution and credential-persistence risks before any further architecture work or deployment. Clean historical local clones and remote E2E artifacts, then restore the service with fail-closed webhook validation, ephemeral Git credentials, hardened Docker execution, authenticated operational APIs, and recoverable jobs.

At the time this phase was approved, it intentionally did not implement the LangGraph execution refactor or Issue-aware AST targeting. Those follow-up items were implemented in the separately approved architecture remediation after the security gate passed.

## Immediate Containment and Cleanup

Perform these actions in order so no stale credential remains usable:

1. Stop the webhook service and any worker process using this repository.
2. Revoke the current GitHub token and create a replacement with the minimum repository permissions required for clone, branch push, Pull Request creation, and Issue comments.
3. Replace the process-level `GITHUB_TOKEN`; never write the replacement to a repository file, command line, log, clone URL, or SQLite record.
4. Delete these local historical roots after resolving and verifying that each target remains under `D:\ximu\chouxiang`:
   - `.runs`
   - `.github-workspaces`
   - `.github-url-workspaces`
5. Recreate only empty runtime roots when the repaired application starts. Historical SQLite data, reports, E2E scripts, logs, and cloned repositories are intentionally discarded.
6. Close GitHub PR #2, delete branch `refactor-agent/issue-1-1783676458`, and close Issue #1.
7. Preserve PR #3 and branch `feat/langgraph-controlled-ast-refactor`, but do not merge or deploy them until the repaired branch passes the security review. Mark PR #3 as draft when GitHub permits it.

## Security Changes

### Webhook Admission

- Treat `GITHUB_WEBHOOK_SECRET` as mandatory for webhook mode. `serve` must fail startup with a clear configuration error when it is absent or empty.
- Reject requests without a valid `X-Hub-Signature-256` before JSON parsing or job creation.
- Require `X-GitHub-Event` to be one of the explicitly supported events and reject unsupported events without creating a job.
- Derive the clone URL from the validated `repository.full_name` as `https://github.com/<owner>/<repo>.git`; never trust `repository.clone_url` or `repository.html_url` as an execution input.
- Validate `repository.full_name` with a strict `owner/repo` grammar and optionally enforce `REFACTOR_AGENT_ALLOWED_REPOSITORIES`, a comma-separated allowlist. Webhook mode must require a non-empty allowlist by default.
- Require `REFACTOR_AGENT_ALLOWED_SENDERS` and reject signed events created by users outside that allowlist.
- Add a maximum webhook body size and bounded Issue/comment text before parsing directives or passing text to the LLM.
- Protect `/jobs` and `/jobs/{id}` with a separate `REFACTOR_AGENT_ADMIN_TOKEN`. Do not reuse the GitHub token.

### Git Authentication

- Remove `_authenticated_clone_url` and never place a token in a URL.
- Clone using the canonical GitHub URL and an ephemeral `GIT_ASKPASS` helper created outside the checkout. Pass the token through the child-process environment only.
- Ensure command errors redact credentials and authorization material.
- Delete the temporary AskPass helper in `finally` and scrub authentication variables from child environments after Git exits.
- Verify after clone that `remote.origin.url` contains no credentials and points to the expected `github.com/<owner>/<repo>` repository.
- Remove each cloned checkout in `finally` after success or failure unless an explicit local debug-retention setting is enabled. Debug retention must default to false in webhook mode.

### Execution Isolation

- Webhook mode must require `sandbox_backend=docker`. `subprocess` and `auto` fallback are rejected at startup for webhook service execution.
- Local CLI commands may retain subprocess mode, but must label it as trusted-code execution rather than a security sandbox.
- Harden Docker runs with:
  - non-root UID/GID
  - `--read-only`
  - writable `tmpfs` for `/tmp`
  - `--cap-drop=ALL`
  - `--security-opt=no-new-privileges`
  - `--pids-limit`
  - network disabled
  - CPU and memory limits
- Mount repository input read-only. Copy only the candidate and test execution tree into a dedicated writable container volume or temporary directory.
- Pass a minimal environment to subprocesses and containers. Never forward GitHub, DeepSeek, webhook, admin, or host credential variables into test execution.
- Apply one total deadline covering container startup, pytest, adversarial tests, mutation tests, and performance profiling.

### Durable Jobs

- Replace FastAPI `BackgroundTasks` execution with a persistent SQLite-backed worker loop.
- Extend job records with serialized validated input, attempt count, lease owner, lease expiry, and idempotency key based on GitHub delivery ID.
- Webhook handlers only validate, persist, and return `202`; a worker atomically leases queued jobs.
- On startup, expired `RUNNING` jobs return to `QUEUED` up to a bounded retry count; exhausted jobs become `FAILED`.
- Reject duplicate GitHub deliveries without starting duplicate branches or Pull Requests.
- Allow only one active job per repository and Issue unless explicitly configured otherwise.

## Public Configuration and Interfaces

Add or change these settings:

- `GITHUB_WEBHOOK_SECRET`: required for `serve`.
- `REFACTOR_AGENT_ALLOWED_REPOSITORIES`: required comma-separated `owner/repo` allowlist for webhook mode.
- `REFACTOR_AGENT_ALLOWED_SENDERS`: required comma-separated GitHub login allowlist for code-changing events.
- `REFACTOR_AGENT_ADMIN_TOKEN`: required for job-query endpoints.
- `REFACTOR_AGENT_SANDBOX_BACKEND`: must equal `docker` in webhook mode.
- `REFACTOR_AGENT_RETAIN_CHECKOUTS`: defaults to `false`; intended only for local debugging.
- `REFACTOR_AGENT_JOB_LEASE_SECONDS`: bounded worker lease duration.
- `REFACTOR_AGENT_JOB_MAX_ATTEMPTS`: bounded durable-job attempts.
- `REFACTOR_AGENT_WEBHOOK_MAX_BYTES`: maximum accepted webhook payload size.

The webhook parser must no longer expose or persist an arbitrary clone URL. `GitHubRefactorJob` stores the validated repository identity and canonical URL derived by the service.

## Test and Review Plan

### Security Tests

- Service startup fails without webhook secret, repository allowlist, admin token, Docker backend, or available Docker daemon.
- Unsigned, incorrectly signed, oversized, unsupported, malformed, and non-allowlisted webhook requests are rejected without database writes.
- A forged payload cannot redirect clone traffic away from `github.com`.
- Git commands never contain tokens; cloned `.git/config` contains no credentials.
- Test processes receive a minimal environment with all known credential variables absent.
- Docker command tests assert every hardening flag, non-root user, read-only filesystem, PID limit, and network isolation.
- `/jobs` endpoints reject missing or invalid admin authentication.

### Reliability Tests

- Duplicate delivery IDs create one job.
- Worker leases are atomic and expired jobs recover after restart.
- Retry exhaustion produces a terminal failure.
- Concurrent events for one Issue do not create duplicate branches or Pull Requests.
- Checkout cleanup runs after success, validation failure, Git failure, LLM failure, timeout, and process cancellation.

### Final Review Gate

- Run the complete unit suite and focused security tests.
- Perform a manual threat-model review of webhook input, child-process environments, filesystem mounts, and credential lifetime.
- Inspect generated Git configuration and process arguments for secrets.
- Run a signed local webhook replay against a disposable private test repository using the replacement least-privilege token.
- Do not push, merge, deploy, or reopen production webhook access until code review finds no critical/high issues.
- Update `README.md`, `plan.md`, and `plan2.md` to remove the inaccurate claims that the current LangGraph layer is the execution orchestrator or that the existing Docker invocation is a strong sandbox.

## Deferred Architecture Work

The decision-complete implementation design is maintained in `architecture-remediation.md`.

After the security phase is accepted:

1. Move actual Minimizer, Defender, Adversary, sandbox, and Judge execution into LangGraph nodes instead of passing precomputed booleans through the graph.
2. Select AST target regions from Issue symbols, paths, failing traceback locations, and complexity rather than complexity alone.
3. Establish a reproducible multi-repository benchmark before publishing aggregate LOC, complexity, or success-rate claims.

## Approval Boundary

Implementation approval authorizes destructive cleanup only for the exact local roots and remote E2E artifacts listed above. It does not authorize deleting PR #3, deleting its branch, merging code, pushing repaired code, deploying services, or modifying `C:\Users\Administrator\.codex\auth.json`.
