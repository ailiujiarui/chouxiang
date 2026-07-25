# README And License Update Design

Date: 2026-07-25  
Status: implemented; review passed

## Goal

Bring repository-facing documentation in line with the current desktop-agent
implementation and make the project available under strong network-copyleft
terms.

## License choice

Adopt `GNU Affero General Public License v3.0 or later` (`AGPL-3.0-or-later`).
It is the strongest widely used OSI-approved copyleft license for this use
case: distributing modified copies requires corresponding source, and running
modified software for remote users triggers a corresponding-source offer.
No open-source license is universally "most strict"; this choice means the
strongest common copyleft option rather than a proprietary source-available
license.

## Planned changes

- Add the unmodified official AGPL-3.0 license text as the root `LICENSE` file.
- Add a concise `AGPL-3.0-or-later` section to `README.md`, including the
  network-use source obligation and a link to `LICENSE`.
- Update `pyproject.toml` package metadata with the SPDX license expression
  and AGPL classifier.
- Correct README activity-listener wording: the Windows collector may read a
  foreground title only in memory to identify sensitive/meeting windows and a
  bounded IDE state. The raw title is discarded before `ActivityEvent`,
  EventBus, SQLite, logs, or remote inference.
- Mark the older title-never-read collector design documents as superseded by
  the implemented 2026-07-25 completion design, while preserving their
  historical scope.

## Verification

- Confirm the license file has the canonical AGPL v3 text.
- Run `python -m compileall -q src tests` and `git diff --check` after
  metadata/documentation edits.

## Verification result

- `LICENSE` matches the official GNU AGPL v3 text.
- README, package metadata, and historical collector wording are synchronized
  with the current implementation.
- `python -m compileall -q src tests`, TOML parsing, and `git diff --check`
  passed.

## Non-goals

- No production-code or runtime behavior changes.
- No legal advice beyond naming and applying the selected license text.
