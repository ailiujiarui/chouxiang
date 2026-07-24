# Nailong Windows Activity Collector Design

## Goal

Add a Windows-first desktop activity collector that emits only minimized foreground application and idle-state signals. It must never read or persist window-title text, editor contents, terminal contents, screenshots, OCR, clipboard data, source code, or credentials.

## Scope

The first release collects foreground-window changes, normalized application categories, idle duration, and fullscreen/meeting signals. It does not implement IDE adapters, terminal parsing, screenshots, OCR, DeepSeek inference, activity classification, or personality responses.

## Architecture

`WindowActivityCollector` runs as a desktop-process background component. A Win32 adapter installs `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` on a dedicated thread with its own message loop. The adapter exposes only a foreground window handle, process ID, and executable name to the collector.

A low-frequency idle sampler supplies idle seconds. The collector combines these signals into a short-lived `ActivityEvent`, immediately passes it to `PrivacyPolicy.admit_activity()`, and only persists or publishes the resulting minimized event.

```text
Win32 foreground hook + idle sampler
  -> WindowActivityCollector
  -> PrivacyPolicy
  -> PrivacyStore
  -> EventBus
```

The renderer, notification subscriber, and analysis service do not call the collector and remain functional if the collector is unavailable.

## Rules

The collector applies gates in this order:

1. Sensitive, meeting, or fullscreen signals reject the candidate.
2. The saved activity-listener switch and manual pause must both allow collection.
3. A blocked application always rejects the candidate.
4. When any allow rules exist, an application must match an allow rule.
5. `PrivacyPolicy` must authorize and minimize the candidate.
6. The collector persists and publishes only the minimized event.

The raw executable path is used only to derive an application category and is discarded. The collector never requests a window title.

## Event Rate and Deduplication

The collector keeps an in-memory key of normalized application category and event kind. It suppresses duplicate foreground transitions for five seconds. The existing privacy store then applies durable fingerprint deduplication and five-minute aggregation windows.

Idle sampling runs no more often than once per minute and emits only when the normalized idle bucket changes. This avoids a database write for every timer tick.

## Lifecycle and Failures

`start()` is idempotent and starts the hook thread and idle sampler. `stop()` first prevents new callbacks, then joins the hook thread before the desktop process stops its event bus.

Hook installation, Win32 process lookup, and callback errors are caught inside the collector. They disable collection and report a generic diagnostic without window titles, paths, or other captured data. No collector failure may terminate the UI, notification delivery, or code-review process.

Non-Windows systems use a no-op adapter so tests and headless operation remain portable.

## Interfaces

The collector depends on small injected protocols:

```python
class ForegroundActivitySource(Protocol):
    def start(self, on_change: Callable[[ForegroundWindow], None]) -> None: ...
    def stop(self) -> None: ...

class IdleStateSource(Protocol):
    def idle_seconds(self) -> int: ...

class WindowActivityCollector:
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

`ForegroundWindow` contains only process ID, executable name, fullscreen state, and meeting likelihood. The collector accepts `PrivacyPolicy`, `PrivacyStore`, `EventBus`, and a preference/rule provider by dependency injection.

## Acceptance Criteria

- Unanswered or declined consent produces no stored or published activity event.
- The collector does not request or store window-title text.
- Manual pause, disabled listening, meeting/fullscreen, and blacklisted applications suppress collection.
- A nonempty allowlist suppresses applications not explicitly allowed.
- The same foreground application is emitted at most once per five-second in-memory window.
- Accepted events are minimized before persistence and event-bus publication.
- Hook installation and callback failures are isolated from the desktop renderer and notification service.
- `stop()` prevents further callbacks and finishes without a live background thread.
- Tests use fake sources and execute without a real Windows window or screenshot.
