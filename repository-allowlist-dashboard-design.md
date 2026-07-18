# Repository Allowlist Dashboard Design

Date: 2026-07-15
Status: Implemented, verified, self-reviewed, and approved for integration on 2026-07-15

## Objective

Add a Chinese Dashboard interface for viewing and managing the GitHub repository allowlist used by Webhook and Dashboard URL jobs. Changes made through the interface must take effect without restarting the API, survive process restarts, and preserve the deployment-controlled environment allowlist.

This is an incremental extension of `phase4-reliability-benchmark-dashboard-design.md`. It changes the previous decision that Dashboard configuration editing was out of scope only for repository allowlist entries; all other runtime settings remain read-only.

## Current Implementation

- `REFACTOR_AGENT_ALLOWED_REPOSITORIES` is parsed once into `AppSettings.allowed_repositories` during process startup.
- `POST /jobs/url` and the GitHub Webhook route validate against that in-memory set.
- `LocalRepositoryRefactorService` repeats the same in-memory check before cloning a Dashboard URL repository.
- The Dashboard can submit an allowlisted URL but cannot display or change the allowlist.
- SQLite has no configuration table, so an interface that only mutates `AppSettings` would be process-local and would be lost after restart.

## Effective Allowlist Model

The effective allowlist is the union of two sources:

1. **Environment entries** from `REFACTOR_AGENT_ALLOWED_REPOSITORIES`.
2. **Dashboard entries** persisted in SQLite.

Repository identities are normalized to lowercase `owner/repository` values and must match GitHub's restricted owner/repository character set. Wildcards, arbitrary hosts, credentials, ports, query strings, fragments, and nested paths are rejected.

Environment entries are deployment-owned and immutable through the API. The Dashboard may add and remove only SQLite-backed entries. If the same repository is present in both sources, it is displayed once with source `ENVIRONMENT` and cannot be removed from the effective allowlist through the Dashboard.

An empty effective allowlist continues to mean deny all.

## Persistence and Policy Boundary

Add a `repository_allowlist` SQLite table:

| Column | Type | Meaning |
| --- | --- | --- |
| `repo_full_name` | text primary key | Lowercase canonical `owner/repository` identity |
| `created_at` | text | UTC creation timestamp |

Add an append-only `repository_allowlist_events` table for local audit evidence:

| Column | Type | Meaning |
| --- | --- | --- |
| `event_id` | text primary key | UUID event identity |
| `action` | text | `ADD` or `REMOVE` |
| `repo_full_name` | text | Affected canonical repository |
| `created_at` | text | UTC timestamp |

Store mutations use `BEGIN IMMEDIATE`. Adding an existing effective entry is idempotent. Removing a missing Dashboard entry is also idempotent, but attempting to remove an environment-owned entry returns `409`.

Introduce one repository policy component backed by `AppSettings` and `SQLiteRunStore`. The Webhook route, Dashboard URL route, and Worker use this same component instead of independently reading `settings.allowed_repositories`.

The Worker rechecks the effective allowlist after claiming a job and before selecting either repository processor. Therefore, removing a Dashboard entry prevents queued jobs for that repository from reaching clone or execution. A job already inside a clone or sandbox operation remains governed by the existing cancellation and deadline controls.

## Admin API

Add Admin Token protected endpoints:

- `GET /admin/repository-allowlist`
- `POST /admin/repository-allowlist`
- `DELETE /admin/repository-allowlist/{owner}/{repository}`

All three endpoints authenticate before reading request content or returning configuration data.

`GET` returns normalized entries with `source`, `removable`, and `created_at`. It does not return environment variable contents beyond the effective repository identities.

`POST` accepts one `repository` value in either canonical `owner/repository` form or canonical `https://github.com/owner/repository` form. It normalizes and validates the value, enforces a maximum of 500 persisted entries, and returns the resulting effective entry.

`DELETE` accepts path segments only after strict validation. Environment-owned entries return `409`; a removed Dashboard entry returns `200`; an already absent entry returns `200` with `removed=false`.

Expected errors are:

- `400` for malformed repository identities or URLs;
- `401` for a missing or invalid Admin Token;
- `409` for attempted removal of an environment-owned entry;
- `413` for an oversized body;
- `422` only for framework-level schema failures that pass the body-size gate.

## Dashboard Interface

Add a `仓库白名单` expander above the existing GitHub URL submission form in the `任务` tab.

When no Admin Token is present, the expander explains that management requires administrator authentication and does not request the allowlist API.

When authenticated, it contains:

- a compact table with repository, source, added time, and removability;
- one repository input accepting `owner/repository` or a canonical GitHub URL;
- an `添加仓库` command button;
- a repository selector and `移除仓库` command button limited to removable Dashboard entries;
- explicit success, validation, authorization, conflict, and connection messages.

The interface does not edit `.env`, process environment variables, command-line arguments, or source files. The Admin Token remains only in Streamlit session state and is sent only to Admin API requests.

After a successful add or remove action, the Dashboard reruns and reloads the server-authoritative list. URL job submission remains a separate form and continues to receive a server-side allowlist check.

## Documentation Changes

Update `README.md` to describe:

- environment entries as immutable deployment defaults;
- Dashboard entries as SQLite-persisted runtime additions;
- the union rule and deny-all empty behavior;
- the requirement for an Admin Token to view or mutate the list;
- removal behavior for queued versus already running jobs.

Update `phase4-reliability-benchmark-dashboard-design.md` after implementation to link this extension and move repository allowlist editing out of the general Dashboard configuration exclusion.

## Tests and Acceptance

### Store and Policy

- Persist, list, and remove Dashboard entries across store instances.
- Normalize case and make duplicate additions idempotent.
- Record append-only add/remove audit events.
- Merge environment and Dashboard sources without duplicates.
- Deny repositories absent from the effective set.
- Prove a removed Dashboard entry is rejected by the Worker before processor dispatch.

### API

- Require a valid Admin Token for list, add, and remove.
- Accept canonical repository names and GitHub HTTPS URLs.
- Reject alternate hosts, credentials, ports, queries, fragments, wildcards, and malformed names.
- Protect environment-owned entries from deletion.
- Enforce request body and persisted-entry limits.
- Prove Webhook and URL submission routes observe additions and removals without restart.

### Dashboard

- Render the Chinese management interface only through the existing thin API client.
- Keep allowlist requests absent in read-only mode.
- Enable add/remove only with an Admin Token and valid server state.
- Show environment entries as non-removable and Dashboard entries as removable.
- Verify successful actions rerun and refresh the list.
- Preserve existing URL submission and task control behavior.

### Final Gate

- Run focused store, policy, Webhook, Worker, API client, and Streamlit AppTest coverage.
- Run the complete test suite.
- Run `compileall`, `git diff --check`, and the existing credential-pattern scan.
- Perform a code review against this design and self-fix all Critical and High findings before requesting any push or deployment approval.

## Out of Scope

- Editing sender, import, branch, organization, or model allowlists.
- Wildcard repositories or organization-wide grants.
- Per-user roles beyond the existing single Admin Token.
- Removing environment-managed entries through the API.
- Automatically cancelling a repository job that is already executing.
- Commit, push, deployment, or modification of historical planning documents.

## Implementation Evidence

- Full automated suite: 233 passed on Python 3.12.
- Focused allowlist, store, Webhook, Worker, Dashboard API, and Streamlit suite: 92 passed.
- URL submission, signed Webhook delivery, and Worker dispatch observe SQLite additions and removals without process restart.
- Streamlit AppTest verifies read-only mode does not request protected configuration and verifies authenticated add/remove refresh behavior.
- Capacity enforcement and insertion execute in one `BEGIN IMMEDIATE` transaction.
- `compileall`, `git diff --check`, and production credential-pattern scans passed.
- Self-review found no unresolved Critical or High issue. The concurrent capacity check and repeated-path-separator validation found during review were fixed before the final suite.
- No commit, push, deployment, or changes outside the allowlist implementation were performed.
