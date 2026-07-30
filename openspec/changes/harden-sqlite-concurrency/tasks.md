## 1. Baseline and write-path audit

- [x] 1.1 Create the implementation branch from latest `main`, carry this OpenSpec change onto it, and record the implementation baseline SHA.
- [x] 1.2 Inventory every production `sqlite3.connect`, write transaction, `INSERT OR REPLACE`, full-row upsert, and database path; map each to main, notification, or privacy ownership.
- [x] 1.3 Record SQLite versions for supported local Python and Docker/CI runtimes, and select at least one CI runtime accepted by the WAL safety gate.

## 2. Shared SQLite runtime policy

- [x] 2.1 Add the standard-library-only shared SQLite policy and diagnostic types with default 5000ms busy timeout and `auto|wal|delete` mode validation.
- [x] 2.2 Implement the shared connection factory so Python timeout, `PRAGMA busy_timeout`, `PRAGMA foreign_keys`, and `sqlite3.Row` are applied and verified on every connection.
- [x] 2.3 Implement one-time per-database journal-mode initialization before worker/background threads start, including actual-mode verification and bounded initialization retry.
- [x] 2.4 Implement the WAL runtime safety gate for fixed SQLite lines, unsupported/unknown runtime handling, and sanitized fallback/fail-closed reasons.
- [x] 2.5 Add common environment/config parsing to `AppSettings` and `NailongSettings`, with direct policy injection for tests.

## 3. Store migration and logical concurrency safety

- [x] 3.1 Migrate `SQLiteRunStore` to the shared policy without changing schema or public read behavior.
- [x] 3.2 Migrate `NotificationStore` and `PrivacyStore` to the same policy and remove their duplicated 30-second connection configuration.
- [x] 3.3 Replace job creation full-row conflict updates with INSERT-only/conflict-read behavior and remove or restrict `save_github_job()` so it cannot overwrite live status or lease fields.
- [x] 3.4 Audit remaining `INSERT OR REPLACE` and full-row upserts; replace unsafe cases with owned-field `ON CONFLICT DO UPDATE` or explicit field methods.
- [x] 3.5 Strengthen claim, transition, cancellation, retry, renewal, completion, notification lease/ACK, and privacy cleanup updates with expected-state/owner conditions and row-count checks.
- [x] 3.6 Verify that all LLM, HTTP, Git, Docker, pytest, UI/event-bus operations and waits occur outside write transaction scopes.

## 4. Diagnostics and operational behavior

- [x] 4.1 Extend safe startup/capability diagnostics with SQLite version, requested/actual journal mode, busy timeout, and WAL gate result without exposing paths or SQL.
- [x] 4.2 Preserve `DATABASE_LOCKED` classification for exhausted waits and add structured, sanitized operation context with no automatic non-idempotent retry.
- [x] 4.3 Update startup and Docker configuration for `auto`, `wal`, and `delete`, and make forced WAL fail before API or Worker acceptance when the gate fails.
- [x] 4.4 Document WAL `-wal`/`-shm` handling, checkpoint-aware backup, controlled rollback to delete mode, and local-filesystem-only support.

## 5. Deterministic concurrency test harness

- [x] 5.1 Add reusable independent-connection lock holders and Barrier/Event orchestration; prohibit fixed long sleeps as the mechanism for creating overlap.
- [x] 5.2 Add connection-policy tests for all three Stores, injected test timeout isolation, actual journal-mode verification, and auto/forced/delete decisions.
- [x] 5.3 Add short-contention and timeout-exhaustion tests that assert elapsed bounds, exact commits, rollback of partial writes, and safe error classification.
- [x] 5.4 Parameterize applicable concurrency tests across rollback journal and safe WAL modes; ensure WAL tests run rather than all being skipped.

## 6. Main database combination tests

- [x] 6.1 Test concurrent API job submission and Worker claim through independent connections, asserting one job, one owner, and coherent audit/event order.
- [x] 6.2 Test concurrent duplicate API submissions, asserting unique delivery/active-job convergence without full-row overwrite.
- [x] 6.3 Test API cancellation racing heartbeat renewal, asserting a legal state and that heartbeat cannot erase cancellation.
- [x] 6.4 Test current-owner completion racing expired-lease recovery or a second Worker, asserting only one legal owner/result.
- [x] 6.5 Test analysis-event emission and heartbeat renewal while API/SSE reads, asserting both writes, unique ordered sequences, and a valid read snapshot.
- [x] 6.6 Add a same-host cross-process reader/writer case representing API/CLI or Dashboard/Worker overlap in WAL mode.

## 7. Desktop and independent-database combination tests

- [x] 7.1 Test notification SSE ingest, delivery lease/ACK, and preference/do-not-disturb updates concurrently, asserting cursor, dedupe, budget, and ACK invariants.
- [x] 7.2 Test privacy activity append racing clear-history and consent update, asserting a complete serialized outcome and preserved consent.
- [x] 7.3 Test main, notification, and privacy writes simultaneously, asserting unrelated database files do not share a process-global serialization lock.

## 8. WAL lifecycle and stress verification

- [x] 8.1 Test WAL stable-snapshot behavior with a long reader and short writer, followed by a new reader observing the commit.
- [x] 8.2 Test restart persistence, controlled checkpoint/rollback to delete mode, and safe handling of database plus `-wal`/`-shm` state.
- [x] 8.3 Add a bounded repeated-contention layer for critical combinations and assert final counts, unique keys, legal states, ownership, event order, and zero unexpected lock errors.
- [x] 8.4 Run targeted Store/API/Worker/desktop concurrency suites in both modes, then run the full pytest suite, compile checks, and diff validation with no unexplained skips or warnings.
