## ADDED Requirements

### Requirement: Unified SQLite connection policy
The system SHALL create all `SQLiteRunStore`, `NotificationStore`, and `PrivacyStore` connections through one shared SQLite policy that applies the configured connection timeout, `PRAGMA busy_timeout`, `PRAGMA foreign_keys`, and row factory on every new connection.

#### Scenario: Every Store receives the same baseline configuration
- **WHEN** each Store opens a fresh independent connection using the default policy
- **THEN** every connection reports the configured busy timeout, enabled foreign keys, and `sqlite3.Row` row behavior

#### Scenario: Test-specific timeout is injected without global mutation
- **WHEN** a concurrency test constructs Stores with an injected short timeout policy
- **THEN** only those Store connections use the test timeout and production defaults remain unchanged

### Requirement: Safe and verifiable journal mode selection
The system SHALL support `auto`, `wal`, and `delete` journal modes per database file and SHALL verify the actual mode returned by SQLite before accepting startup.

#### Scenario: Auto mode enables WAL on a safe local runtime
- **WHEN** `auto` mode opens a local database using a SQLite runtime recognized as containing the WAL concurrency fix and `PRAGMA journal_mode=WAL` returns `wal`
- **THEN** startup succeeds and diagnostics report actual mode `wal`

#### Scenario: Auto mode safely falls back
- **WHEN** `auto` mode detects an unrecognized runtime, unsupported filesystem, or unsuccessful WAL negotiation and the database is not already in WAL mode
- **THEN** the database remains in rollback journal mode, startup continues, and diagnostics record a structured fallback reason

#### Scenario: Unsafe runtime cannot join an existing WAL database
- **WHEN** `auto` mode finds that the database is already in WAL mode but the current SQLite runtime does not pass the safety gate
- **THEN** startup fails before accepting work and instructs the operator to upgrade the runtime or perform a controlled rollback with all writers stopped

#### Scenario: Forced WAL fails closed
- **WHEN** `wal` mode cannot pass the runtime, filesystem, or returned-mode checks
- **THEN** startup fails before API or Worker threads accept work and reports an actionable non-secret error

#### Scenario: Delete mode remains available for rollback
- **WHEN** an operator selects `delete` while no writers are active
- **THEN** the database reports `journal_mode=delete` and continues using the shared busy timeout policy

### Requirement: Bounded lock waiting
The system SHALL wait for transient SQLite write contention for at most the configured busy timeout and SHALL classify timeout exhaustion without partial domain writes.

#### Scenario: Competing writer succeeds before timeout
- **WHEN** one connection holds a write lock and releases it before another connection's busy timeout expires
- **THEN** the waiting operation completes exactly once and both committed domain changes are present

#### Scenario: Lock wait expires predictably
- **WHEN** a write lock remains held beyond the configured timeout
- **THEN** the waiting operation returns the structured database-locked outcome within a bounded timing tolerance and leaves no partial rows or state transitions

### Requirement: Short write transaction boundary
The system MUST keep database write transactions limited to local SQL, validation of already-loaded values, and bounded in-memory transformation; it MUST NOT perform LLM, HTTP, Git, Docker, pytest, UI, event-bus delivery, sleep, or thread/process waits while a write transaction is open.

#### Scenario: Worker executes expensive work outside its lease transaction
- **WHEN** a Worker claims a job, performs repository analysis, and records completion
- **THEN** claim and completion use separate short transactions and all external processing occurs after the claim commit and before the completion transaction

#### Scenario: Desktop delivery does not hold a database lock during UI publication
- **WHEN** the delivery pump leases an intent and publishes it to the UI event bus
- **THEN** the lease transaction commits before event-bus publication and ACK uses a later independent transaction

### Requirement: Atomic state and ownership updates
The system SHALL protect task, lease, notification, and privacy state with INSERT-only creation or conditional UPDATE/CAS statements and SHALL reject stale owners without overwriting current state.

#### Scenario: Concurrent duplicate job submissions converge
- **WHEN** two independent API connections submit the same delivery or active job concurrently
- **THEN** exactly one job is created, both callers resolve to that job, and no existing status or lease field is overwritten

#### Scenario: Only one Worker claims a queued job
- **WHEN** two Workers attempt to claim the same queued job concurrently
- **THEN** one Worker becomes the owner, the other receives no claim, and exactly one claim/state event is persisted

#### Scenario: Stale Worker cannot complete reclaimed work
- **WHEN** a lease is reclaimed and the previous owner later attempts completion or renewal
- **THEN** the stale operation changes zero rows and returns a lease-lost/domain-conflict outcome

#### Scenario: Cancel and heartbeat race preserves the state machine
- **WHEN** an API cancellation and Worker heartbeat target the same running job concurrently
- **THEN** the final state is a legal serialized outcome and a heartbeat cannot revert or erase cancellation

### Requirement: Main database cross-module concurrency
The system SHALL preserve domain invariants when API, Worker, heartbeat, analysis-event, CLI, and Dashboard access overlap on the main database.

#### Scenario: API submission overlaps Worker claim
- **WHEN** API submission and Worker polling write the main database through independent connections with controlled overlap
- **THEN** the submitted job is eventually visible, claimed at most once, and neither participant fails from transient contention shorter than the timeout

#### Scenario: Event emission overlaps lease renewal and API reading
- **WHEN** analysis-event emission and heartbeat renewal write concurrently while an API connection reads events
- **THEN** both writes commit once, event sequences remain unique and ordered, and the reader observes a valid snapshot

#### Scenario: Completion overlaps lease recovery
- **WHEN** the current owner completes while another Worker checks for expired leases
- **THEN** the database serializes the operations to one legal terminal or reclaimed outcome without lost audit events

### Requirement: Desktop storage concurrency
The system SHALL preserve notification and privacy invariants when desktop background threads and user controls access their respective SQLite files concurrently.

#### Scenario: Notification ingest, delivery, and settings overlap
- **WHEN** SSE ingestion, delivery lease/ACK, and preference or do-not-disturb updates execute through independent notification connections
- **THEN** notification intents remain deduplicated, popup budget and cursor updates are not lost, and each ACK applies at most once

#### Scenario: Activity append overlaps privacy clearing
- **WHEN** activity persistence and clear-history execute concurrently on the privacy database
- **THEN** the result matches one complete serialized order, activity tables remain consistent, and consent is preserved

#### Scenario: Independent databases write simultaneously
- **WHEN** the main Store, NotificationStore, and PrivacyStore each write their own database at the same time
- **THEN** all three commits succeed independently and no process-global SQLite lock serializes unrelated files

### Requirement: WAL read and checkpoint behavior
When actual journal mode is WAL, the system SHALL permit readers to use stable snapshots during short writes and SHALL retain SQLite's bounded automatic checkpoint behavior unless a separately reviewed policy replaces it.

#### Scenario: Reader continues during a writer transaction
- **WHEN** a reader begins before a concurrent writer commits in WAL mode
- **THEN** the reader completes against its stable snapshot and a later read observes the committed change

#### Scenario: Journal files are handled as database state
- **WHEN** a WAL-mode database is backed up, moved, or rolled back to delete mode
- **THEN** the operation uses a controlled stop/checkpoint procedure and never copies or removes the main file independently of required WAL state

### Requirement: SQLite concurrency diagnostics
The system SHALL expose non-secret diagnostics for SQLite version, requested and actual journal mode, configured busy timeout, and WAL safety-gate result, and SHALL retain the existing safe database-lock error classification.

#### Scenario: Startup reports effective configuration
- **WHEN** a Store initializes a database file
- **THEN** logs or the approved capabilities surface report effective SQLite settings without exposing the absolute database path or SQL payloads

#### Scenario: Lock timeout remains safely classified
- **WHEN** a Worker database operation exhausts busy timeout
- **THEN** the job or operation records `DATABASE_LOCKED` with a sanitized summary and no raw SQL, secrets, or filesystem path

### Requirement: Deterministic concurrency verification
The test suite MUST include deterministic integration tests that use independent connections and synchronization primitives to create real overlapping operations in both rollback-journal and safe WAL environments.

#### Scenario: Thread-level combination matrix runs in CI
- **WHEN** the standard CI suite runs
- **THEN** it executes API/Worker, Worker/heartbeat/event, notification, privacy, timeout, and independent-database combinations using barriers or events rather than sequential calls or timing guesses

#### Scenario: WAL coverage cannot be entirely skipped
- **WHEN** CI evaluates WAL-specific behavior
- **THEN** at least one job uses a SQLite runtime accepted by the WAL safety gate and runs reader/writer plus cross-process WAL scenarios

#### Scenario: Stress layer validates aggregate invariants
- **WHEN** the repeated contention test layer runs a bounded number of iterations
- **THEN** final row counts, unique keys, legal states, ownership, event order, and absence of unexpected `database is locked` errors are asserted
