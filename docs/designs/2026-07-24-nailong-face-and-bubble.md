# Nailong Face And Status Bubble Design

Date: 2026-07-24
Status: implemented; review passed

## Goal

Make the Nailong desktop pet visibly expressive using parenthesized literal
Chinese labels: show two `（眼睛）` labels and one mouth-state label directly
in the pet body, replace the mouth label with words such as `（大笑）`, and show
idle/working/result states in a speech bubble above the pet.

## Current implementation

- `PySide6Renderer` renders one rounded `QLabel` with static text.
- Popup decisions are temporary labels parented to the pet window, not a
  persistent status bubble above it.
- `PopupDecision` already carries an action, message, priority, and display
  duration, but no explicit facial expression.
- The existing event bus and headless renderer must remain usable without
  PySide6.

## Design

### Text face layer

Keep the existing QLabel-based body and replace its static text with three
literal text regions. No custom painting, emoticons, emoji, or image assets
are used:

```text
（眼睛）          （眼睛）
          （嘴巴）
```

The two eye labels remain `（眼睛）`. The mouth region displays one bounded
parenthesized state word: `（嘴巴）`, `（微笑）`, `（大笑）`, `（担忧）`, or
`（困倦）`. Expression changes only replace this mouth-region word. The face
labels remain separate from the status bubble text.

### Pet state

Add a bounded `PetExpression` enum and a `PetState` model with:

- `expression`: neutral, happy, laugh, concerned, sleepy;
- `bubble_text`: optional short text;
- `bubble_visible`: boolean;
- `bubble_seconds`: bounded display duration.

`PopupDecision` remains compatible. Its `message` and `display_seconds` map
to the bubble, while a small deterministic mapping chooses a text face from
the action/reason (`show`, `defer`, `drop`, success/failure/idle keywords).

### Bubble layer

Create a frameless bubble QLabel owned by the desktop process but positioned
above the pet window. It must:

- follow the pet when the window moves;
- wrap text within a stable maximum width;
- use a light background with a visible border and a small pointer;
- auto-hide after the requested duration;
- replace the previous bubble instead of stacking indefinitely.

The bubble is the primary display for idle, thinking, working, completed, and
failed states. The face remains readable even when the bubble is hidden.

### Headless behavior

`NullRenderer` continues recording visible `PopupDecision` values for tests.
It also records the mapped expression so state transitions can be tested
without creating a GUI.

## Tests

- expression mapping covers show/defer/drop and common status reasons;
- headless renderer records the selected expression and visible bubble;
- invalid/empty messages do not crash rendering;
- repeated decisions replace the current bubble state;
- existing event bus, lock, and headless lifecycle tests remain green;
- PySide6 smoke test constructs and closes the renderer when the dependency is
  available.

## Acceptance

- The pet visibly has two eyes and a mouth on startup.
- A laugh expression visibly changes the mouth-region text from `（嘴巴）` to
  `（大笑）`.
- Status text appears in a bubble above the pet and auto-hides.
- The desktop process remains independently launchable with
  `python -m nailong_agent`.
- No analysis engine, API contract, or privacy behavior changes.

## Non-goals

- No automatic activity monitoring or code submission in this iteration.
- No full animation or image asset pipeline; text face changes are enough for
  the first expressive implementation.

## Verification

- Literal face labels render as `（眼睛）`, `（嘴巴）`, `（微笑）`,
  `（大笑）`, `（担忧）`, and `（困倦）`.
- Startup displays `（待机中）` in the bubble above the pet for 30 seconds.
- PySide6 offscreen construction/start/stop smoke passed.
- `pytest -q`: 222 passed; one existing Starlette/httpx deprecation warning.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
