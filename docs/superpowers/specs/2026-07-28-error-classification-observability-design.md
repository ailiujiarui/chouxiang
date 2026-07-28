# Error Classification and Observability Design

## Goal

Classify operational failures consistently, make them diagnosable without exposing internals, and give users stable, actionable error messages.

## Scope

This change covers the refactor execution path: LLM transport and response handling, SQLite persistence, background job processing, generated failure reports, dashboard API errors, and Streamlit error rendering. It does not attempt to replace unrelated broad exception handlers in the desktop activity subsystem.

## Error Contract

Introduce a shared application error contract with three separate representations:

| Consumer | Stored information |
| --- | --- |
| UI | Stable error code and a code-mapped user-safe message. Never display exception text, HTTP response detail, file paths, source, credentials, or a traceback. |
| Logs | Error code, operation name, job/run identifiers, and the exception chain with traceback through the logger. Do not log prompts, source code, tokens, or credentials. |
| Database | Error code, public message, and a bounded technical summary suitable for filtering and support. Do not store tracebacks or arbitrary raw exception text. |

The contract exposes a stable code and a safe public message. Its internal diagnostic context is used only for structured logging and for producing a bounded, sanitized database summary.

## Error Codes

The code set includes the existing LLM classifications and adds persistence-specific classifications:

| Code | Meaning | Public message |
| --- | --- | --- |
| `LLM_AUTH_FAILED` | Provider rejected credentials. | The language model credentials were rejected. Check the configured credential and try again. |
| `RATE_LIMITED` | Provider rate limit exhausted after retries. | The language model is busy. Try again shortly. |
| `SERVER_ERROR` | Provider returned a retryable server failure. | The language model is temporarily unavailable. Try again later. |
| `TIMEOUT` | Provider request timed out after retries. | The language model request timed out. Try again. |
| `CLIENT_ERROR` | Provider transport or non-auth client failure. | The language model request could not be completed. Try again. |
| `PARSE_ERROR` | Provider response was invalid for the expected contract. | The language model returned an invalid result. Try again. |
| `INPUT_TOO_LARGE` | Request exceeds the accepted input limit. | The submitted content is too large. Reduce it and try again. |
| `INJECTION_DETECTED` | Untrusted input matched a prompt-injection safeguard. | The submitted content could not be processed safely. |
| `DATABASE_LOCKED` | SQLite rejected an operation because the database is locked or busy. | The local database is busy. Try again shortly. |
| `INTERNAL_ERROR` | An unexpected failure reached an isolation boundary. | The task could not be completed because of an internal error. |

The enum member names and serialized values are identical. New code must use the serialized code rather than matching exception messages.

## Propagation and Isolation

LLM client methods continue to translate known `httpx` failures to typed LLM errors. Their messages become internal diagnostics, while their public message comes from the shared code mapping.

SQLite access translates only `sqlite3.OperationalError` instances whose normalized message indicates `locked` or `busy` to `DATABASE_LOCKED`. Integrity and transition conflicts retain their existing typed behavior. Other SQLite errors are allowed to reach the worker isolation boundary and become `INTERNAL_ERROR`.

The job worker catches expected cancellation and deadline conditions as it does today. It catches the shared operational error type to persist its code, public message, and technical summary. The one top-level worker-loop guard remains broad to protect the thread, but logs with `logger.exception` and does not expose or persist its raw exception. Other `except Exception` handlers touched by this flow are narrowed to their actual expected exception families; no broad catch is retained merely to turn `str(exc)` into task output.

## Persistence and Reports

Job/run persistence gains fields for `error_code`, `error_message`, and `error_summary`. Migration is backward compatible: old rows with only the legacy error value map to `INTERNAL_ERROR` with a generic public message and no raw legacy content is shown in the UI.

Generated reports and API payloads expose only `error_code` and `error_message`. The technical summary remains in the database for restricted operational inspection and is not included in dashboard responses or artifacts.

## UI Behavior

The dashboard maps every known error code to its public message. HTTP status context may be shown when it is itself safe, but response `detail` is never concatenated into the visible error. Unknown or missing codes use the generic internal-error message. Existing error displays for run records use the persisted public message, not the historical free-form error field.

## Tests

Focused tests will prove that:

1. LLM 401 and exhausted 429 responses return `LLM_AUTH_FAILED` and `RATE_LIMITED` with safe public messages.
2. A SQLite locked/busy operational error becomes `DATABASE_LOCKED` and persists a bounded summary without a traceback.
3. An unexpected worker failure is logged as an internal error and persists only the generic public message and code.
4. Dashboard formatting does not render supplied API detail, traceback fragments, or raw database summaries.
5. Existing successful job and dashboard behavior remains unchanged.

## Non-goals

This change does not add remote telemetry, alter retry policy, change database retention, or refactor unrelated exception handling outside the refactor execution flow.
