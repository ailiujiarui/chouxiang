# Nailong Window Activity Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect minimized Windows foreground and idle signals through an isolated, privacy-gated background component.

**Architecture:** `windows_activity.py` owns Win32 event-hook and idle adapters; `activity_collector.py` owns lifecycle, gating, throttling, persistence, and event publication. `DesktopProcess` creates the collector only when enabled and stops it before its event bus. Tests inject fake sources and never require a real window.

**Tech Stack:** Python 3.11, ctypes Win32 APIs, Pydantic, sqlite3, pytest.

---

### Task 1: Define Platform-Neutral Collection Boundary

**Files:**
- Create: `src/nailong_agent/activity_collector.py`
- Create: `tests/test_activity_collector.py`

- [ ] **Step 1: Write failing collector tests**

```python
def test_collector_persists_and_publishes_only_privacy_minimized_event(tmp_path):
    source = FakeForegroundSource()
    collector, store, bus, received = make_collector(tmp_path, source, consent=True)
    collector.start()
    source.emit(ForegroundWindow(process_id=1, executable_name="Code.exe"))
    assert bus.wait_idle(1)
    assert store.activity_count() == 1
    assert received[0].payload["application_id"] == "code"
    collector.stop()
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_activity_collector.py -q`

Expected: import failure because the collector module does not exist.

- [ ] **Step 3: Implement protocols and collector**

Define `ForegroundWindow`, `ForegroundActivitySource`, and `IdleStateSource`. Implement `WindowActivityCollector.start()`/`stop()` idempotently. On a foreground callback, construct an event without a title or path, check listener preference/manual pause/application rules, call `PrivacyPolicy.admit_activity()`, persist the resulting event, and publish its envelope.

- [ ] **Step 4: Add gates and throttle tests**

Add tests for unanswered consent, manual pause, disabled listener, blocked app, nonmatching allowlist, and a duplicate application emitted within five seconds. Each must leave the store and event subscriber unchanged.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest tests/test_activity_collector.py tests/test_nailong_privacy.py -q`

Expected: PASS.

Commit `activity_collector.py` and its test with message `feat: add privacy-gated activity collector`.

### Task 2: Add Windows Hook and Idle Adapters

**Files:**
- Create: `src/nailong_agent/windows_activity.py`
- Modify: `tests/test_activity_collector.py`

- [ ] **Step 1: Write failing adapter tests**

```python
def test_non_windows_source_is_a_noop(monkeypatch):
    monkeypatch.setattr(windows_activity.os, "name", "posix")
    source = windows_activity.create_foreground_source()
    source.start(lambda _: pytest.fail("unexpected callback"))
    source.stop()
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_activity_collector.py -q`

Expected: missing adapter factory.

- [ ] **Step 3: Implement adapters**

Use `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` with a dedicated message-loop thread on Windows. Resolve only process ID and executable basename, never a window title. Use `GetLastInputInfo` for idle seconds and a monitor-rectangle comparison for fullscreen. Provide no-op sources on non-Windows systems. Catch hook and callback errors, disable the source, and report a generic error callback.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_activity_collector.py -q`

Expected: PASS.

Commit `windows_activity.py` and tests with message `feat: add Windows activity hook adapter`.

### Task 3: Wire the Collector into the Desktop Lifecycle

**Files:**
- Modify: `src/nailong_agent/app.py`
- Modify: `src/nailong_agent/config.py`
- Modify: `src/nailong_agent/__init__.py`
- Modify: `tests/test_nailong_scaffold.py`

- [ ] **Step 1: Write a failing lifecycle test**

```python
def test_desktop_process_starts_and_stops_injected_activity_collector(tmp_path):
    collector = CollectorProbe()
    process = DesktopProcess(lock_path=tmp_path / "nailong.lock", renderer_factory=NullRenderer, activity_collector=collector)
    assert process.run() == 0
    assert (collector.starts, collector.stops) == (1, 1)
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_nailong_scaffold.py -q`

Expected: `DesktopProcess` does not accept `activity_collector`.

- [ ] **Step 3: Implement lifecycle wiring**

Add `activity_collector` injection to `DesktopProcess`. Create and start it after privacy consent is resolved and before renderer execution. Stop it before event-bus shutdown. Add `NAILONG_ACTIVITY_LISTENER_ENABLED` as a default setting and use it only to decide whether `main()` creates the collector; persisted preference remains the runtime authority.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/test_activity_collector.py tests/test_nailong_scaffold.py tests/test_nailong_privacy.py -q`

Expected: PASS.

Commit lifecycle files and tests with message `feat: wire Nailong activity collection`.

### Task 4: Document and Verify

**Files:**
- Modify: `README.md`
- Modify: `docs/designs/2026-07-24-nailong-configuration-persistence-task.md`

- [ ] **Step 1: Document controls and limits**

Document the foreground/idle-only scope, explicit authorization, listener switch, application rules, no-title/no-screenshot guarantee, and Windows-only hook implementation.

- [ ] **Step 2: Run final verification**

Run: `python -m pytest tests/test_activity_collector.py tests/test_nailong_config.py tests/test_nailong_privacy.py tests/test_nailong_scaffold.py -q`, `python -m compileall -q src tests`, and `git diff --check`.

Expected: all commands succeed.

- [ ] **Step 3: Commit documentation**

Commit documentation with message `docs: document Nailong activity collection`.
