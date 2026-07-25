# Test Suite Simplification Design

Date: 2026-07-25  
Status: implemented; review passed

## Goal

Reduce redundant test cases in the current Nailong desktop and activity test
coverage while preserving the behavioral contracts that protect privacy,
pause controls, event aggregation, and the text-only renderer.

## Scope

- Keep one focused test for each newly added desktop behavior.
- Merge the window-title and IDE-hint checks into one privacy-boundary test.
- Keep the durable manual-pause persistence test as the source of truth; remove
  only duplicate callback-only assertions that do not exercise persistence.
- Keep one renderer click test proving the callback is local and does not call
  an LLM; retain existing face-expression and bubble tests.
- Do not remove tests covering sensitive-window blocking, consent, event
  deduplication/window aggregation, SQLite migration, or notification policy.
- Do not change production code or alter network behavior as part of this
  cleanup.

## Expected result

The suite should lose a small number of overlapping tests (roughly 3-5), with
the same core requirements covered by fewer, clearer cases. The design and
test names should make the remaining coverage easy to locate.

## Verification

- Run the affected test modules and confirm they pass.
- Run collection to record the reduced test count.
- Run compile and diff checks.

## Verification result

- Affected desktop, collector, and notification tests: 47 passed.
- Full collection: 369 tests (three redundant cases removed).
- `compileall` and `git diff --check` passed.

## Non-goals

- No broad deletion of historical tests solely to lower the count.
- No weakening of privacy or persistence assertions.
- No push or deployment as part of the cleanup.
