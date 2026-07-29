# Error Classification and Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give failures stable error codes and distinct safe representations for users, logs, and SQLite records.

**Architecture:** A shared error module owns serialized codes, public messages, bounded summaries, and SQLite lock classification. LLM, persistence, workers, reports, and the dashboard pass that contract instead of raw exception text. Legacy database `error` values are cleared during migration.

**Tech Stack:** Python 3.11, Pydantic 2, SQLite, FastAPI, httpx, Streamlit, pytest.

---

## File Structure

- Create: `src/refactor_agent/errors.py` - serialized code enum, typed error, public-message mapping, summary sanitizer, SQLite lock classifier.
- Modify: `src/refactor_agent/llm.py`, `models.py`, `store.py` - classify LLM failures and persist code, public message, and bounded summary.
- Modify: `orchestrator.py`, `local_repository.py`, `snippet.py`, `job_worker.py` - propagate typed errors and narrow touched broad catches.
- Modify: `dashboard_api.py`, `dashboard_views.py`, `dashboard.py`, `webhook.py` - discard API detail and render public messages only.
- Modify: `benchmark_runner.py`, `memory.py`, `demo_suite.py` - consume codes rather than raw error strings.
- Modify: `tests/test_llm.py`, `tests/test_store.py`, `tests/test_orchestrator.py`, `tests/test_dashboard.py`; create `tests/test_job_worker.py`.

### Task 1: Define and Verify the Error Contract

**Files:**
- Create: `src/refactor_agent/errors.py`
- Modify: `src/refactor_agent/llm.py`
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write the failing public-contract test**

```python
from refactor_agent.errors import ErrorCode, public_error_message

def test_error_codes_have_safe_public_messages() -> None:
    assert ErrorCode.LLM_AUTH_FAILED.value == "LLM_AUTH_FAILED"
    assert ErrorCode.RATE_LIMITED.value == "RATE_LIMITED"
    assert ErrorCode.DATABASE_LOCKED.value == "DATABASE_LOCKED"
    assert "DEEPSEEK_API_KEY" not in public_error_message(ErrorCode.LLM_AUTH_FAILED)
```

- [ ] **Step 2: Run the test and verify the intended failure**

Run: `pytest tests/test_llm.py::test_error_codes_have_safe_public_messages -q`

Expected: FAIL because `refactor_agent.errors` does not exist.

- [ ] **Step 3: Implement the minimal typed contract**

```python
class ErrorCode(StrEnum):
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CLIENT_ERROR = "CLIENT_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    INJECTION_DETECTED = "INJECTION_DETECTED"
    DATABASE_LOCKED = "DATABASE_LOCKED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

class OperationalError(RuntimeError):
    def __init__(self, code: ErrorCode, summary: str = "") -> None:
        super().__init__(code.value)
        self.code = code
        self.summary = sanitize_error_summary(summary)

    @property
    def public_message(self) -> str:
        return public_error_message(self.code)
```

Implement `sanitize_error_summary` to cap length, collapse whitespace, and strip line breaks. Implement `is_database_locked(exc)` to accept only normalized SQLite `locked` or `busy` messages. Change `LLMError` to subclass `OperationalError`; preserve `LLMErrorCode` as a compatible import alias and replace `AUTH_FAILED` references with `LLM_AUTH_FAILED`.

- [ ] **Step 4: Run the focused module**

Run: `pytest tests/test_llm.py -q`

Expected: PASS, including existing 401 and exhausted-429 coverage.

- [ ] **Step 5: Commit**

```powershell
git add src/refactor_agent/errors.py src/refactor_agent/llm.py tests/test_llm.py
git commit -m "feat: add structured operational errors"
```

### Task 2: Migrate and Persist Safe Database Fields

**Files:**
- Modify: `src/refactor_agent/models.py`
- Modify: `src/refactor_agent/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write failing migration and round-trip tests**

```python
def test_store_migrates_legacy_raw_error_to_generic_error(tmp_path: Path) -> None:
    database = _legacy_database_with_error(
        tmp_path, "Traceback (most recent call last): private-token"
    )
    record = SQLiteRunStore(database).get("legacy-1")
    assert record.error_code == ErrorCode.INTERNAL_ERROR
    assert record.error_message == public_error_message(ErrorCode.INTERNAL_ERROR)
    assert record.error_summary is None

def test_store_round_trips_structured_error(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    store.save(_failed_run(ErrorCode.DATABASE_LOCKED, "sqlite database is locked"))
    loaded = store.get("run-locked")
    assert loaded.error_code == ErrorCode.DATABASE_LOCKED
    assert loaded.error_message == public_error_message(ErrorCode.DATABASE_LOCKED)
    assert loaded.error_summary == "sqlite database is locked"
```

- [ ] **Step 2: Run the tests and verify the intended failure**

Run: `pytest tests/test_store.py::test_store_migrates_legacy_raw_error_to_generic_error tests/test_store.py::test_store_round_trips_structured_error -q`

Expected: FAIL because structured fields and migration do not exist.

- [ ] **Step 3: Add fields, schema columns, and migration**

Add these fields to `RunRecord`, `GitHubAutomationResult`, and `GitHubJobRecord`:

```python
error_code: ErrorCode | None = None
error_message: str | None = None
error_summary: str | None = None
```

Add the same nullable columns to `runs` and `github_jobs`. Extend `_migrate_runs_metadata` and `_migrate_github_jobs` to add missing columns. For every legacy non-null `error`, write `INTERNAL_ERROR` and its generic public message, clear `error_summary`, and set legacy `error` to `NULL`. Change SQL insert/update and row reconstruction to use the new columns; only `error_summary` is sanitized before persistence.

- [ ] **Step 4: Run the store module**

Run: `pytest tests/test_store.py -q`

Expected: PASS, including existing lifecycle and migration tests.

- [ ] **Step 5: Commit**

```powershell
git add src/refactor_agent/models.py src/refactor_agent/store.py tests/test_store.py
git commit -m "feat: persist classified operational errors"
```

### Task 3: Propagate Failures Without Raw Text

**Files:**
- Modify: `src/refactor_agent/orchestrator.py`
- Modify: `src/refactor_agent/local_repository.py`
- Modify: `src/refactor_agent/snippet.py`
- Modify: `src/refactor_agent/job_worker.py`
- Test: `tests/test_orchestrator.py`
- Create: `tests/test_job_worker.py`

- [ ] **Step 1: Write failing LLM and unknown-worker failure tests**

```python
def test_orchestrator_persists_safe_llm_error(tmp_path: Path) -> None:
    result = _orchestrator_with(
        LLMError(ErrorCode.LLM_AUTH_FAILED, "provider token=private")
    ).run(_request(tmp_path))
    assert result.record.error_code == ErrorCode.LLM_AUTH_FAILED
    assert result.record.error_message == public_error_message(ErrorCode.LLM_AUTH_FAILED)
    assert "private" not in (result.record.error_message or "")

def test_worker_persists_generic_error_without_exception_text(caplog) -> None:
    worker = _worker_with_processor_raising(ValueError("Traceback private-value"))
    worker.run_once()
    record = worker.store.get_github_job("job-1")
    assert record.error_code == ErrorCode.INTERNAL_ERROR
    assert "private-value" not in (record.error_message or "")
    assert "private-value" in caplog.text
```

- [ ] **Step 2: Run the tests and verify the intended failure**

Run: `pytest tests/test_orchestrator.py::test_orchestrator_persists_safe_llm_error tests/test_job_worker.py::test_worker_persists_generic_error_without_exception_text -q`

Expected: FAIL because the current paths persist `str(exc)`.

- [ ] **Step 3: Classify at containment boundaries and narrow catches**

Store `OperationalError` in orchestration state and create run records from `code`, `public_message`, and `summary`. Copy those three fields to repository and snippet job results. In `GitHubJobWorker.run_once`, catch `OperationalError` before the unknown exception guard, and convert unknown exceptions to `INTERNAL_ERROR` while calling `logger.exception` with `job_id` and `error_code`. Catch `sqlite3.OperationalError` separately; only locked/busy maps to `DATABASE_LOCKED`, and other SQLite failures go to the generic containment guard. Replace touched nested broad catches with `JobTransitionError`; retain only the top-level thread-containment guard and log there with `logger.exception`.

- [ ] **Step 4: Run execution regressions**

Run: `pytest tests/test_orchestrator.py tests/test_job_worker.py -q`

Expected: PASS with raw exception text present only in captured logs.

- [ ] **Step 5: Commit**

```powershell
git add src/refactor_agent/orchestrator.py src/refactor_agent/local_repository.py src/refactor_agent/snippet.py src/refactor_agent/job_worker.py tests/test_orchestrator.py tests/test_job_worker.py
git commit -m "fix: keep internal errors out of task records"
```

### Task 4: Redact API, UI, Reports, and String-Based Consumers

**Files:**
- Modify: `src/refactor_agent/dashboard_api.py`
- Modify: `src/refactor_agent/dashboard_views.py`
- Modify: `src/refactor_agent/dashboard.py`
- Modify: `src/refactor_agent/webhook.py`
- Modify: `src/refactor_agent/benchmark_runner.py`
- Modify: `src/refactor_agent/memory.py`
- Modify: `src/refactor_agent/demo_suite.py`
- Test: `tests/test_dashboard.py`, `tests/test_control_api.py`, `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing UI and report-redaction tests**

```python
def test_dashboard_errors_never_include_api_detail() -> None:
    detail = "Traceback (most recent call last): /private/token"
    rendered = dashboard_views.format_dashboard_error(503, detail)
    assert "Worker" in rendered
    assert detail not in rendered
    assert "Traceback" not in rendered

def test_failed_report_includes_code_and_public_message_only(tmp_path: Path) -> None:
    result = _orchestrator_with(
        LLMError(ErrorCode.LLM_AUTH_FAILED, "token=private")
    ).run(_request(tmp_path))
    assert "LLM_AUTH_FAILED" in result.report_markdown
    assert public_error_message(ErrorCode.LLM_AUTH_FAILED) in result.report_markdown
    assert "token=private" not in result.report_markdown
```

Also add an API-client test where `{"error_code": "DATABASE_LOCKED", "detail": "secret"}` yields a `DashboardApiError` with the recognized code but no retained detail.

- [ ] **Step 2: Run the tests and verify the intended failure**

Run: `pytest tests/test_dashboard.py tests/test_orchestrator.py -q`

Expected: FAIL because dashboard formatting appends detail and reports render raw errors.

- [ ] **Step 3: Render only code-mapped public messages**

Give `DashboardApiError` an optional `ErrorCode`. For HTTP responses, parse only a recognized `error_code` and discard `detail` and response text. Make `format_dashboard_error` accept that optional code and return the shared public message when known; otherwise return only the status-safe summary. Pass the code through `_show_dashboard_error`, and render `error_message` rather than legacy error text in run/job views. API serializers must exclude `error_summary`.

Update report building to show the code and public message only. Update benchmark, memory, and demo consumers to use `error_code` and `error_message`, falling back to `INTERNAL_ERROR` without matching raw strings.

- [ ] **Step 4: Run targeted end-to-end verification**

Run: `pytest tests/test_dashboard.py tests/test_control_api.py tests/test_orchestrator.py -q`

Expected: PASS with no API detail, traceback, or technical summary rendered.

- [ ] **Step 5: Run static and diff checks, then commit**

Run: `python -m compileall -q src tests`

Expected: exit code 0.

Run: `git diff --check`

Expected: no output and exit code 0.

```powershell
git add src/refactor_agent/dashboard_api.py src/refactor_agent/dashboard_views.py src/refactor_agent/dashboard.py src/refactor_agent/webhook.py src/refactor_agent/benchmark_runner.py src/refactor_agent/memory.py src/refactor_agent/demo_suite.py tests/test_dashboard.py tests/test_control_api.py tests/test_orchestrator.py
git commit -m "fix: redact errors from reports and dashboard"
```

### Task 5: Final Focused Verification

**Files:**
- Test: `tests/test_llm.py`, `tests/test_store.py`, `tests/test_orchestrator.py`, `tests/test_job_worker.py`, `tests/test_dashboard.py`, `tests/test_control_api.py`

- [ ] **Step 1: Run the complete targeted suite**

Run: `pytest tests/test_llm.py tests/test_store.py tests/test_orchestrator.py tests/test_job_worker.py tests/test_dashboard.py tests/test_control_api.py -q`

Expected: PASS.

- [ ] **Step 2: Verify repository state before handoff**

Run: `git status --short`

Expected: no uncommitted tracked changes after the implementation commits; no permission or environment file is staged.

- [ ] **Step 3: Inspect committed changes**

Run: `git show --check --stat HEAD`

Expected: no whitespace errors and only code, tests, and approved documentation in the final commit.

