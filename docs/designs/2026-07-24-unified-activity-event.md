# Nailong Unified Activity Event Design

Date: 2026-07-24
Status: implemented; review passed

## Goal

Complete the unified desktop activity event layer for Nailong. Every admitted
activity event uses one privacy-minimized schema, duplicate signals are
suppressed, and accepted signals are combined into bounded time windows before
downstream classification or persistence.

## Current implementation findings

- `ActivityEvent` already has timestamp, source, application, and sensitivity,
  but activity and confidence are split into `ActivityClassification`.
- Raw collection fields (`window_title_summary`, `activity_hint`, arbitrary
  `metadata`) still exist on the event model.
- `PrivacyPolicy` minimizes events before storage and blocks sensitive/meeting
  content, but it does not expose the requested unified schema.
- `PrivacyStore` rejects non-minimized events and stores only an allowlisted
  metadata subset. It has event-id idempotency but no semantic deduplication or
  activity time-window aggregation.
- Notification deduplication is separate and does not solve desktop activity
  signal deduplication.

## Unified model

Add `ActivityType` and make the persisted/admitted `ActivityEvent` contract:

```text
event_id
occurred_at       # timezone-aware UTC timestamp
source            # window | process | idle | ide
application_id    # normalized application category, not path/title
activity          # coding | debugging | reading | writing | meeting |
                  # gaming | media | idle | unknown
confidence        # 0.0 .. 1.0
summary           # optional redacted summary, max 240 characters
sensitivity       # public | private | blocked
```

The model uses `extra="forbid"`. There are deliberately no fields for raw
source code, clipboard contents, screenshots, OCR text, full window titles,
terminal text, commands, or arbitrary metadata.

For migration, raw collectors use a separate `RawActivitySignal` model. It may
temporarily carry a window-title hint for local privacy classification, but it
cannot be persisted, published to the EventBus, or sent to a remote model.
`PrivacyPolicy.admit_activity()` converts a raw signal into the unified event.

## Privacy behavior

- Collection remains fail-closed until explicit local consent exists.
- Sensitive or meeting signals are rejected before unified-event creation.
- Application values are normalized to categories such as `code`, `terminal`,
  or `browser`; executable paths are never stored.
- `summary` is generated from allowlisted facts and passed through redaction.
- An empty safe summary is preferred over retaining raw text.
- SQLite accepts only public unified events with normalized applications and
  valid activity/confidence values.

## Deduplication

Add `ActivityEventAggregator` after the privacy gate. Its semantic fingerprint
is:

```text
source + application_id + activity + normalized summary
```

Identical fingerprints inside a configurable 5-second deduplication interval
are counted but not emitted as separate events. Event IDs remain independently
idempotent at the store boundary.

## Time-window aggregation

The aggregator groups admitted events into configurable 60-second tumbling
windows. An `ActivityWindow` contains:

- UTC `window_started_at` and `window_ended_at`;
- dominant application and activity;
- confidence weighted by accepted event count;
- accepted event count and duplicate count;
- at most one redacted summary;
- sensitivity fixed to `public`.

A window is emitted when a later event crosses the boundary or when `flush()`
is called. Empty windows are not emitted. Events arriving older than the
active window are rejected to keep ordering deterministic.

## Persistence migration

Extend `pet_activity_events` with `activity`, `confidence`, and `summary`.
Existing rows migrate to `activity='unknown'`, `confidence=0.0`, and a null
summary. Remove new writes to idle/fullscreen/meeting metadata columns while
leaving legacy columns readable for existing databases.

Add `pet_activity_windows` for aggregated windows with a unique window/fact
key. No raw collector payload is stored in either table.

Databases created by the intermediate five-minute activity-window
implementation are migrated to this unified window schema. Existing aggregate
counts and confidence are preserved; their historical window duration remains
five minutes, while newly emitted windows use the configured 60-second size.

## Tests

- unified model accepts exactly the requested fields and rejects raw-code,
  clipboard, screenshot, full-title, and arbitrary metadata fields;
- naive timestamps, out-of-range confidence, unsafe application IDs, and
  oversized summaries are rejected;
- privacy conversion strips/redacts raw signal text before returning an event;
- same semantic event inside five seconds is deduplicated;
- different activity/application signals are not incorrectly deduplicated;
- 60-second windows produce deterministic dominant activity, confidence,
  counts, and summary;
- out-of-order events and explicit flush behavior are covered;
- SQLite migration preserves existing rows and stores only unified events and
  aggregated windows;
- clearing local activity history deletes events and windows together.

## Acceptance

- All downstream desktop activity consumers receive the unified event schema.
- Raw code, clipboard, screenshots, OCR, full window titles, and arbitrary
  metadata have no default persistence path.
- Semantic deduplication and time-window aggregation are deterministic and
  configurable.
- Existing privacy, notification, event bus, and desktop lifecycle tests remain
  green.
- Full code review confirms that notification-event deduplication remains
  separate from activity-signal deduplication.

## Non-goals

- No screenshot, clipboard, OCR, keylogging, source-code, or terminal collector.
- No remote inference implementation in this change.
- No change to analysis-task notification cooldown or terminal-event policy.

## Implementation review

- Implemented the unified event, ephemeral raw-signal boundary, five-second
  semantic deduplication, 60-second aggregation, and backward-compatible
  SQLite migration described above.
- Review tightened application minimization: known executables map to fixed
  categories and unknown names become `other`, so customer- or project-specific
  process names do not enter persisted summaries.
- Verification on 2026-07-24 after integrating remote changes: 304 tests passed;
  Python bytecode compilation,
  Docker Compose configuration validation, and `git diff --check` passed.
