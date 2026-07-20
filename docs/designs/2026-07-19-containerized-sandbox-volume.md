# Containerized Sandbox Volume Design

Date: 2026-07-19
Status: implemented; review passed

## Problem

The API service runs inside Docker and stores run workspaces in the Compose
named volume mounted at `/data`. When that service invokes the host Docker
daemon for a sandbox, a bind mount such as `/data/runs/...:/workspace` refers
to a path that does not exist in the daemon's host namespace. The sandbox then
starts with an empty workspace and pytest cannot find the generated files.

## Design

- Let the API pass the Compose named volume to sandbox containers when it is
  running in the containerized deployment.
- Mount that volume read-only at `/data` in the sandbox and keep the existing
  read-only, no-network, dropped-capability restrictions.
- Derive the sandbox working directory and target paths from the workspace's
  `/data` relative path; keep local subprocess and direct bind-mount behavior
  unchanged.
- Configure the volume name explicitly in Compose so the API and sandbox use
  the same project-scoped volume.

## Acceptance

- A containerized snippet task can locate `snippet.py` and `test_snippet.py` in
  the Docker sandbox.
- No sandbox network access or writable source mount is introduced.
- Existing subprocess sandbox tests and direct Docker command tests remain
  valid.
