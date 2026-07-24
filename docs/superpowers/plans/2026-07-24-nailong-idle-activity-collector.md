# Nailong Idle Activity Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish one privacy-gated minimized idle event when the user remains inactive for five minutes, without reading desktop content.

**Architecture:** `windows_activity.py` adds a Windows-only polling source backed by `GetLastInputInfo`; the source emits only when idle time first crosses 300 seconds and resets after activity resumes. `activity_collector.py` receives its callback, applies consent and persisted listener/pause gates, then stores and publishes an `ActivityEvent(source="idle", application_id="system")`. Both Win32 sources stop after an internal callback failure and report only a generic error.

**Tech Stack:** Python 3.11, ctypes Win32 APIs, Pydantic, sqlite3, pytest.

---

### Task 1: Add a Platform-Neutral Idle Source Boundary

**Files:**
- Modify: `src/nailong_agent/activity_collector.py`
- Modify: `tests/test_activity_collector.py`

- [ ] **Step 1: Write the failing idle collection test**

```python
idle_source.emit(IdleState(idle_seconds=300))

assert store.activity_count() == 1
assert received[0].payload["source"] == "idle"
assert received[0].payload["application_id"] == "system"
assert received[0].payload["metadata"] == {"idle_seconds": 300}
```

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/test_activity_collector.py::test_collector_persists_idle_state_after_threshold -q`

Expected: FAIL because `IdleState` and `idle_source` are unavailable.

- [ ] **Step 3: Implement the boundary and collector callback**

```python
@dataclass(frozen=True)
class IdleState:
    idle_seconds: float

class IdleStateSource(Protocol):
    def start(self, on_idle: Callable[[IdleState], None]) -> None: ...

    def stop(self) -> None: ...
```

Accept `idle_source: IdleStateSource | None` in `WindowActivityCollector`. Start and stop it with the foreground source. Its callback must apply `activity_listener_enabled`, `manual_pause_enabled`, and `PrivacyPolicy.admit_activity()` before persisting and publishing `ActivityEvent(source="idle", application_id="system", metadata={"idle_seconds": state.idle_seconds})`.

- [ ] **Step 4: Verify the test passes**

Run: `python -m pytest tests/test_activity_collector.py::test_collector_persists_idle_state_after_threshold -q`

Expected: `1 passed`.

### Task 2: Add the Windows Polling Adapter and Failure Containment

**Files:**
- Modify: `src/nailong_agent/windows_activity.py`
- Modify: `tests/test_activity_collector.py`
- Modify: `src/nailong_agent/app.py`

- [ ] **Step 1: Write failing threshold and failure tests**

```python
source = WindowsIdleStateSource(read_idle_seconds=lambda: 300, poll_interval_seconds=0)
source._sample_once()
source._sample_once()
assert observed == [IdleState(idle_seconds=300)]

source._handle_callback_failure(RuntimeError("private details"))
assert errors == ["activity source failed"]
assert source.stopped
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_activity_collector.py -q`

Expected: FAIL because the idle adapter and generic failure handling do not exist.

- [ ] **Step 3: Implement a one-shot idle threshold source**

Implement `WindowsIdleStateSource` with a daemon polling thread, a default 15-second interval, a 300-second threshold, and injected idle reader for tests. The source emits once per continuous idle period, resets when observed idle seconds fall below 300, and never reads titles, paths, code, clipboard, screenshots, or OCR. `create_idle_source()` returns a no-op source outside Windows. Pass that source from `main()` into `WindowActivityCollector`.

- [ ] **Step 4: Stop a failed source without exposing internal details**

On a foreground callback error, set the source stop event and call `on_error(RuntimeError("activity source failed"))`. Apply the same behavior to idle callback and reader failures. Keep a test-visible `stopped` property rather than asserting private thread state.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest tests/test_activity_collector.py tests/test_nailong_scaffold.py tests/test_nailong_privacy.py -q`

Run: `python -m compileall -q src tests`

Run: `git diff --check`

Expected: all commands succeed without running the full suite.

Commit: `feat: collect Nailong idle activity`
