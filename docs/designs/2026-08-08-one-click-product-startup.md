# One-Click Product Startup Design

Date: 2026-08-08
Status: superseded by the host-first startup design

> The first implementation was replaced after a real startup attempt failed
> while installing `docker-cli` through apt. The current implementation is
> specified in `2026-08-08-host-first-one-click-startup.md`.

## Goal

Make the normal Windows startup path launch the usable product with one action:
API, Dashboard, sandbox image, and Nailong desktop pet. First-run setup may do
the required bootstrap work; later starts must be idempotent and fast.

## User entry points

- Add a root `start.cmd` suitable for double-clicking. It invokes the signed-in
  user's PowerShell with the internal `scripts/start.ps1` and preserves a
  readable error on failure.
- Add a root `stop.cmd` that invokes `scripts/stop.ps1`.
- Expose no advanced command-line options. Remove `-Build`, `-Down`, `-Follow`,
  `-Desktop`, port, image, package-index, and data-directory switches.
- Starting always launches API, Dashboard, and Nailong, then opens Dashboard.
- Stopping always closes this repository's Nailong process and Compose project
  while preserving data.

## First-run bootstrap

- Verify Docker CLI, Compose, and Docker Desktop before changing state.
- Build the sandbox and application images on every start. Docker layer caching
  keeps unchanged restarts fast while ensuring a pulled code update cannot run
  against stale images.
- Use a repository-local `.venv` for Nailong. If its desktop dependencies are
  missing, create the venv and install `.[desktop]` once using the configured
  package index. Do not mutate the user's global Python environment.
- Fail with a precise recovery command when Python, venv creation, package
  installation, Docker build, or health checks fail.

## Configuration

- Add a committed `.env.example` and ignore local `.env`. This is the only
  supported user configuration surface.
- Load only an allowlisted set of startup variables from `.env` into the host
  process so Docker services and Nailong see the same DeepSeek/model/settings
  values. Never print secret values.
- Real DeepSeek remains the default. Without `DEEPSEEK_API_KEY`, the stack may
  start for diagnostics but LLM task entry points remain disabled, matching
  `/capabilities`; mock mode still requires explicit
  `REFACTOR_AGENT_MOCK_LLM=true`.

## Idempotency and lifecycle

- Use fixed localhost ports (`8000` API and `8501` Dashboard). Re-running
  startup must not reject ports owned by this Compose project.
- Ports used by unrelated processes still fail before startup with an explicit
  message.
- Nailong remains single-instance; startup reports whether it started a new
  process or an existing instance is already active.
- After both health checks pass, always open the Dashboard in the default
  browser.
- `stop.cmd`/`scripts/stop.ps1` stop Compose and the Nailong process started for
  this repository, without deleting volumes, databases, or run artifacts.

## Documentation synchronization

- Rewrite the README quick start around `start.cmd` and `stop.cmd`; PowerShell
  scripts are implementation details rather than advanced user interfaces.
- Update `docker/README.md` and mark the outdated default-mock/default-token
  statements in `2026-07-18-one-click-start.md` as superseded.

## Verification

- Contract tests cover full-product startup, `.env` allowlisting,
  image/bootstrap logic, idempotent reruns, safe stop, and no secret output.
- PowerShell syntax parsing, focused tests, Compose validation, compileall, and
  `git diff --check` pass.
- When Docker Desktop is available, run a smoke start, verify API and Dashboard
  health, verify Nailong single-instance behavior, then stop without deleting
  persisted data.

## Non-goals

- No Linux/macOS launcher in this change.
- No installer, Windows service, autostart-at-login, or packaged executable.
- No storage of DeepSeek keys in SQLite or source-controlled files.

## Verification result

- PowerShell syntax parsing passed for both startup and shutdown scripts.
- Startup/CI contract tests: 8 passed.
- Full suite: 573 passed, 11 skipped, with one existing Starlette/httpx
  deprecation warning.
- Compose validation, compileall, and `git diff --check` passed.
- Docker Desktop was not running in the verification environment, so the
  real container/Desktop smoke remains pending; the launcher fails early with
  an actionable Docker Desktop message in that condition.
