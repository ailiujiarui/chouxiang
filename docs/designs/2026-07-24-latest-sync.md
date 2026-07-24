# Latest Code Synchronization Design

Date: 2026-07-24
Status: implemented; review passed

## Goal

Confirm that the working tree contains the latest code from `origin/main`,
and document any implementation work required when the remote branch has
advanced.

## Audit findings

- Local branch was `main` at `c04abfc`, twelve commits behind `origin/main`.
- `main` was fast-forwarded to `f4e6472`.
- The remote update added the Nailong privacy boundary, analysis event stream,
  proactive notification pipeline, richer speech bubble, and `-Desktop`
  startup support.
- Existing local persona and literal-face work was saved in Git stash before
  the fast-forward, then reapplied onto the updated implementation.
- Reapplication produced conflicts in `renderer.py` and
  `test_nailong_scaffold.py`; they were resolved by preserving the remote
  notification/privacy behavior and layering the literal face state onto it.

## Proposed update procedure

1. Before implementation, re-check `origin/main` and the working tree.
2. If the remote branch advances, fast-forward `main` only when it has no
   local divergence; otherwise stop and report the conflict for approval.
3. If the remote branch is unchanged, do not modify application code.
4. Run the repository verification commands after any fast-forward:
   `pytest -q`, `python -m compileall -q src tests`,
   `docker compose config --quiet`, and `git diff --check`.
5. Review the resulting design/code alignment and record any residual gaps.

## Non-goals

- No feature implementation or dependency upgrade is implied by this audit.
- No push, deployment, branch rewrite, or destructive Git operation.

## Acceptance

- `main` is equal to `origin/main`, or any divergence is explicitly reported.
- Verification results are recorded after code changes or fast-forward.
- No application code is edited before this design receives approval.

## Verification

- Local `main` and `origin/main` both resolve to
  `f4e6472b18efb2eca67fdf9cccf8e51a8c31b85c` before local working changes.
- Focused post-merge tests: 47 passed.
- `pytest -q`: 254 passed; one existing `httpx`/Starlette deprecation warning.
- `python -m compileall -q src tests`: passed.
- `docker compose config --quiet`: passed.
- `git diff --check` and staged diff checks: passed.
