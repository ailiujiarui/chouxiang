# Nailong Configuration and Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, durable Nailong desktop configuration, activity persistence, and notification preferences.

**Architecture:** A desktop-only `NailongSettings` resolves CLI arguments, saved preferences, environment defaults, and code defaults. It exposes optional runtime preference overrides, which `NotificationService` overlays on the durable `PetPreferences` row without rewriting that row. Privacy/activity data stays in `nailong_privacy.sqlite`; notification, preference, and personality state stays in `nailong_notifications.sqlite`. The desktop process derives both stores and its lock from one data directory.

**Tech Stack:** Python 3.11, Pydantic, `sqlite3`, argparse, pytest, PowerShell.

---

### Task 1: Add Settings and Unified Desktop Paths

**Files:**
- Create: `src/nailong_agent/config.py`
- Modify: `src/nailong_agent/app.py`
- Modify: `src/nailong_agent/__init__.py`
- Create: `tests/test_nailong_config.py`

- [ ] **Step 1: Write failing settings tests**

```python
def test_settings_derive_paths_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NAILONG_DATA_DIR", str(tmp_path / "pet-data"))
    settings = NailongSettings.from_env()
    assert settings.lock_path == tmp_path / "pet-data" / "nailong-agent.lock"
    assert settings.privacy_database == tmp_path / "pet-data" / "nailong_privacy.sqlite"
    assert settings.notification_database == tmp_path / "pet-data" / "nailong_notifications.sqlite"

def test_explicit_path_override_wins_over_derived_path(tmp_path):
    settings = NailongSettings(data_dir=tmp_path)
    assert settings.with_overrides(notification_database=tmp_path / "override.sqlite").notification_database == tmp_path / "override.sqlite"
```

- [ ] **Step 2: Verify failure**

Run `python -m pytest tests/test_nailong_config.py -q`. Expect an import failure because `nailong_agent.config` does not exist.

- [ ] **Step 3: Implement `NailongSettings`**

Create a Pydantic model with `data_dir`, `analysis_url`, `deepseek_model`, optional path overrides, and optional runtime preference overrides. Add `from_env()`, `with_overrides()`, and derived `lock_path`, `privacy_database`, and `notification_database` properties. Parse `NAILONG_DATA_DIR`, `NAILONG_ANALYSIS_URL`, `NAILONG_DEEPSEEK_MODEL`, `NAILONG_MAXIMUM_POPUPS_PER_DAY`, `NAILONG_MINIMUM_COOLDOWN_SECONDS`, and `NAILONG_MAXIMUM_COOLDOWN_SECONDS`; never model or persist an API key.

- [ ] **Step 4: Route the entry point through settings**

Keep `--lock-path` and `--notification-database` compatibility flags; add `--data-dir` and `--privacy-database`. Construct `PrivacyStore(settings.privacy_database)`, `NotificationService.from_database(settings.notification_database)`, and `DesktopProcess(lock_path=settings.lock_path, ...)`. Preserve injected stores in `DesktopProcess` tests.

- [ ] **Step 5: Verify and commit**

Run `python -m pytest tests/test_nailong_config.py tests/test_nailong_scaffold.py -q`; expect PASS. Commit only the four files in this task with message `feat: add Nailong desktop settings`.

### Task 2: Persist Minimized Activity Dedupe and Windows

**Files:**
- Modify: `src/nailong_agent/privacy_store.py`
- Modify: `tests/test_nailong_privacy.py`

- [ ] **Step 1: Write failing persistence tests**

```python
def test_store_deduplicates_and_aggregates_minimized_activity(tmp_path):
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    event = ActivityEvent(source="window", application_id="code")
    classification = ActivityClassification(activity="coding", confidence=0.9)
    assert store.append_minimized_activity(event, classification=classification) is True
    assert store.append_minimized_activity(event, classification=classification) is False
    assert store.list_activity_windows()[0].event_count == 1
```

- [ ] **Step 2: Verify failure**

Run `python -m pytest tests/test_nailong_privacy.py -q`. Expect a missing classification argument or window-query API.

- [ ] **Step 3: Implement safe event and aggregate storage**

Extend `append_minimized_activity()` with optional `ActivityClassification`. Persist only normalized public fields, activity type, confidence, and a SHA-256 fingerprint. Use `INSERT OR IGNORE` for event/fingerprint dedupe. Add `pet_activity_windows`, keyed by application, activity, and a five-minute UTC window, and upsert count, last timestamp, and max confidence. Add a typed `ActivityWindow` return type for the query API.

- [ ] **Step 4: Migrate and delete safely**

Add legacy `activity`, `confidence`, and `fingerprint` columns only when absent. `clear_activity_history()` must delete both event and aggregate rows but retain consent. Enable `foreign_keys` and `busy_timeout` per connection. Add a direct SQLite legacy-schema test that preserves consent through migration.

- [ ] **Step 5: Verify and commit**

Run `python -m pytest tests/test_nailong_privacy.py -q`; expect PASS. Commit `privacy_store.py` and its test with message `feat: persist minimized Nailong activity windows`.

### Task 3: Add Durable Preferences and Daily Popup Budget

**Files:**
- Modify: `src/nailong_agent/events.py`
- Modify: `src/nailong_agent/notification_store.py`
- Modify: `src/nailong_agent/notification_service.py`
- Modify: `tests/test_notification_pipeline.py`

- [ ] **Step 1: Write failing preference and budget tests**

```python
def test_daily_budget_is_durable_and_only_counts_displaying_intents(tmp_path):
    store = NotificationStore(tmp_path / "notifications.sqlite")
    store.save_preferences(PetPreferences(maximum_popups_per_day=1))
    service = NotificationService(store=store, clock=fixed_clock)
    _enqueue_two_intents(service)
    assert service.lease_next() is not None
    assert service.lease_next() is None
    assert NotificationService(store=NotificationStore(store.database_path), clock=fixed_clock).get_status().remaining_daily_popup_budget == 0
```

Add reload tests for manual pause, DND schedule, cooldown range, personality intensity, and blocked application rules.

- [ ] **Step 2: Verify failure**

Run `python -m pytest tests/test_notification_pipeline.py -q`. Expect missing preference and daily-budget APIs.

- [ ] **Step 3: Define persisted types and tables**

Add `PetPreferences` and `PetApplicationRule` models. Preferences include listener and manual-pause flags, DND start/end, cooldown range, maximum popups per day, and `LOW`/`STANDARD`/`HIGH` personality intensity. Create idempotent `pet_preferences`, `pet_app_rules`, `pet_personality_state`, and `notification_daily_budget` tables. Add store APIs to get/save preferences and list/replace application rules; reject an inverted cooldown range.

- [ ] **Step 4: Apply controls atomically**

At enqueue and lease time, suppress proactive notifications while manually paused or in scheduled DND. In `lease_next_intent()`, atomically check and increment the local-date budget before transitioning to `DISPLAYING`. Do not count queued, dropped, or acknowledged notifications. Preserve existing explicit-DND terminal summary behavior. Extend `NotificationStatus` with manual pause, scheduled DND, and remaining budget fields. Have `NotificationService` compute effective preferences by overlaying the optional `NailongSettings` runtime overrides on the durable preferences; this implements CLI/runtime override > persisted preference > environment default > code default without overwriting user choices.

- [ ] **Step 5: Add migration and date-boundary coverage**

Open a current-schema notification database and confirm cursor, DND, and pending intents survive. Test a cross-midnight DND interval and a new local date receiving a fresh budget.

- [ ] **Step 6: Verify and commit**

Run `python -m pytest tests/test_notification_pipeline.py tests/test_nailong_scaffold.py -q`; expect PASS. Commit the four files with message `feat: persist Nailong notification preferences`.

### Task 4: Wire Startup and Document the Contract

**Files:**
- Modify: `scripts/start.ps1`
- Modify: `tests/test_startup_contract.py`
- Modify: `README.md`
- Modify: `docs/designs/2026-07-24-nailong-configuration-persistence-task.md`

- [ ] **Step 1: Write failing startup coverage**

```python
def test_desktop_startup_passes_shared_nailong_data_directory():
    script = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    assert "NailongDataDir" in script
    assert '"--data-dir",' in script
```

- [ ] **Step 2: Verify failure**

Run `python -m pytest tests/test_startup_contract.py -q`. Expect failure because the script only hard-codes a notification database path.

- [ ] **Step 3: Forward the shared data directory**

Add a `NailongDataDir` PowerShell parameter defaulting to `.runs`; pass `--data-dir` to the desktop process. Remove the hard-coded notification database argument. Settings owns all filenames under the directory.

- [ ] **Step 4: Document safe operation**

Document the three `NAILONG_*` environment variables, the lock/privacy/notification files, and the prohibition on persisting API keys, source code, raw desktop content, screenshots, OCR, clipboard, and terminal bodies. Mark implementation status in the task document only after final verification passes.

- [ ] **Step 5: Verify and commit**

Run `python -m pytest tests/test_nailong_config.py tests/test_nailong_privacy.py tests/test_notification_pipeline.py tests/test_nailong_scaffold.py tests/test_startup_contract.py -q`, `python -m compileall -q src tests`, and `git diff --check`; expect all pass. Commit the four files with message `docs: document Nailong desktop data settings`.
