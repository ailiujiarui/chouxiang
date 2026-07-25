# Nailong Remaining Desktop Completion Design

Date: 2026-07-25  
Status: implemented; review passed

## Scope

Complete the non-animation gaps in the desktop-pet task list. Existing PNG,
GIF, and animation-resource work remains explicitly out of scope.

## Pause and listener controls

- Add a tray action for `暂停监听` backed by the durable
  `pet_preferences.manual_pause_enabled` field.
- The action must update the same `NotificationService` preference path used
  by the collector and notification policy, so a restart preserves the state.
- Keep `免打扰` separate from listener pause: pause prevents collection and
  proactive notifications; DND suppresses notifications while collection may
  continue.
- Expose the current pause state to the tray action and headless test renderer.

## Ephemeral window and IDE recognition

- Extend the Windows foreground source with an in-memory window-title hint and
  local IDE-state hint (`coding`, `debugging`, or `unknown`).
- Read these values only on the Windows hook thread and pass them into
  `RawActivitySignal` for local privacy checks and classification.
- `PrivacyPolicy` remains the only boundary that can create `ActivityEvent`.
  The title hint is discarded before persistence, EventBus publication, and
  remote inference; sensitive and meeting markers still fail closed.
- Keep executable/application categories normalized. Do not persist full
  titles, file paths, editor filenames, terminal bodies, screenshots, or OCR.

## Desktop interaction

- Add a non-blocking click callback for the pet window that displays a fixed
  local acknowledgement through the existing bubble path.
- Add a close action through the existing tray/quit path; renderer callbacks
  remain free of LLM calls.
- Preserve the text-only eyes/mouth implementation and current bubble layout.

## Verification

- Add tests for pause persistence and tray callback wiring.
- Add Windows-source tests proving title/IDE hints are ephemeral and never
  appear in the admitted event or serialized payload.
- Add renderer interaction tests for click acknowledgement and close callback.
- Run the full test suite, compileall, Compose validation, and diff checks.

## Acceptance

- Task-list items 1, 3, 7, 8, 9, and 10 are fully represented in the running
  desktop path, except for explicitly excluded animation resources.
- No new persistence or remote path contains raw window titles or IDE text.
- Manual pause, DND, privacy consent, and sensitive-window blocking retain
  their existing precedence.

Animation assets remain intentionally excluded. All other listed desktop
completion items are implemented in the current branch.

## Verification

- Focused desktop, collector, notification, renderer, and Windows-source
  tests: 78 passed.
- Full suite after implementation: 369 passed, 1 environment-dependent CLI
  failure. The remaining failure is the pre-existing stdin snippet review test
  when the real DeepSeek endpoint returns HTTP 400; it is unrelated to this
  desktop completion.
- `compileall` and `git diff --check` passed.
