# Local Single-User Authentication Design

Date: 2026-07-20
Status: implemented; review passed

## Goal

Remove the mandatory Admin Token from the local single-user code analysis
workflow so the user can paste code, submit a repository URL, and inspect
results without copying a token into Dashboard.

## Scope

- Public local read endpoints: health, capabilities, jobs, runs, trajectory,
  and artifacts.
- Local analysis submission: `/analysis`, `/jobs/snippet`, and `/jobs/url`.
- Dashboard no longer asks for or sends an Admin Token for normal analysis.
- Repository URL safety still relies on canonical URL validation and the
  existing allowlist policy.

## Management boundary

- Repository allowlist mutation remains protected when
  `REFACTOR_AGENT_ADMIN_TOKEN` is configured.
- Job cancellation and retry remain protected when a token is configured.
- When no token is configured, the local single-user deployment treats these
  operations as local-admin actions; the startup script must make this mode
  explicit and must not imply network security.
- The API is still bound to localhost by the one-click startup defaults.

## Compatibility

- Existing clients that send a valid Bearer token continue to work.
- Existing deployments with a configured token retain enforcement for
  management operations.
- Invalid tokens are rejected whenever token protection is enabled.

## Acceptance

- Default local startup has no Admin Token input in Dashboard.
- A default local `/analysis` request succeeds without Authorization.
- Configured-token management tests still enforce Authorization.
- README, startup output, capabilities, and tests describe local single-user
  mode accurately.
