# Docker Build Reliability Design

Date: 2026-07-19
Status: implemented; review passed

## Problem

The application image installs the full editable package after copying the
entire source tree. Any source change invalidates the dependency layer and
forces a fresh download of the large dashboard and LLM dependency graph.
The default public PyPI route is usable but can be slow enough to make the
one-click startup appear hung.

## Design

- Add a configurable `PIP_INDEX_URL` build argument with the public PyPI URL
  as the default. Users may select a reachable mirror without editing files.
- Add pip retry and timeout defaults so a slow or unavailable index fails with
  a useful error instead of waiting indefinitely.
- Keep the image dependency installation reproducible and do not silently
  install from an arbitrary host path.
- Preserve the current read-only repository mount and local-only runtime
  behavior.
- Expose the same build argument through `scripts/start.ps1` and Compose.

## Acceptance

- `docker compose config --quiet` succeeds with and without a custom index.
- The startup script can pass a custom index to both the app and sandbox
  builds.
- A build using a reachable mirror completes, and the resulting image starts
  API and Dashboard health checks.
- No GitHub write path or runtime security boundary changes.
