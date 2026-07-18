# 运维仪表盘中文化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将四页签 Streamlit 运维仪表盘的固定用户可见内容中文化，同时保持 API、数据库、状态机和控制请求契约不变。

**Architecture:** 中文化集中在展示层。`dashboard_views.py` 提供状态、表格和错误提示的纯转换函数，`dashboard.py` 只负责渲染中文控件；所有业务判断继续使用原始英文状态。

**Tech Stack:** Python 3.12、Streamlit、httpx、pytest、Streamlit AppTest。

## Global Constraints

- 固定使用简体中文，不引入语言切换器或通用 i18n 框架。
- API URL、Job ID、Run ID、URL、文件名、源码、diff 和日志正文保持原样。
- 已知状态显示为“中文（原值）”，未知状态显示为“未知状态（原值）”。
- 控制按钮是否可用只由原始英文状态决定。
- 不修改 FastAPI、SQLite、Worker、Docker 或 GitHub 行为。
- 使用测试驱动开发，每个生产行为先观察到对应测试按预期失败。
- 保留当前工作区全部既有改动，不修改 `plan.md`、`plan2.md`。
- 完成后先执行完整代码审查与自我修复；不提交、不推送、不部署。

---

### Task 1: 集中式中文视图模型

**Files:**
- Modify: `tests/test_dashboard.py`
- Modify: `src/refactor_agent/dashboard_views.py`

**Interfaces:**
- Produces: `localize_status(status: object) -> str`。
- Produces: `format_dashboard_error(status_code: int | None, detail: str) -> str`。
- Produces: `build_task_table_rows(rows: list[TaskRow]) -> list[dict[str, Any]]`。
- Produces: `build_timeline_rows(items: list[TimelineItem]) -> list[dict[str, Any]]`。
- Produces: `build_benchmark_run_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]`。
- Updates: `build_execution_rows()` and `build_benchmark_rows()` return Chinese column names while preserving raw identifiers and diagnostic text.

- [ ] **Step 1: Write failing localization tests**

Add these assertions:

```python
def test_dashboard_status_labels_keep_raw_values():
    assert localize_status("RUNNING") == "运行中（RUNNING）"
    assert localize_status("MINIMIZER_PROPOSED") == "精简者已提交方案（MINIMIZER_PROPOSED）"
    assert localize_status("CUSTOM") == "未知状态（CUSTOM）"


def test_dashboard_tables_use_chinese_columns_without_translating_ids():
    rows = build_task_rows([{"job_id": "job-1", "status": "RUNNING"}])
    assert build_task_table_rows(rows)[0]["任务 ID"] == "job-1"
    assert build_task_table_rows(rows)[0]["状态"] == "运行中（RUNNING）"
    assert set(build_execution_rows([{"attempt": 1, "agent": "JUDGE", "status": "SUCCESS"}])[0]) == {
        "轮次", "Agent", "状态", "消息", "奖励分"
    }


def test_dashboard_errors_add_chinese_context_and_keep_detail():
    assert format_dashboard_error(409, "terminal job") == (
        "当前状态不允许执行该操作。详细信息：terminal job"
    )
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
pytest tests/test_dashboard.py -q
```

Expected: FAIL because the new translation and table functions do not exist, and existing table keys remain English.

- [ ] **Step 3: Implement minimal pure translation helpers**

Add fixed maps for job statuses and currently emitted trajectory statuses. Implement unknown fallback without changing the raw value:

```python
def localize_status(status: object) -> str:
    raw = str(status or "UNKNOWN")
    label = STATUS_LABELS.get(raw)
    return f"{label}（{raw}）" if label else f"未知状态（{raw}）"
```

Build Chinese dictionaries for task, event, execution, benchmark-run, and benchmark-case tables. Do not include internal `can_cancel` or `can_retry` fields in rendered task rows.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
pytest tests/test_dashboard.py -q
```

Expected: all Dashboard tests pass.

---

### Task 2: Streamlit 四页签中文界面

**Files:**
- Modify: `tests/test_dashboard.py`
- Modify: `src/refactor_agent/dashboard.py`

**Interfaces:**
- Consumes: Task 1 localization and table-row helpers.
- Preserves: `DashboardApiClient` request format and Admin Token handling.
- Produces: Chinese page title, sidebar, tabs, metrics, selectors, controls, empty states, artifact headings, and API error context.

- [ ] **Step 1: Update AppTest expectations before production code**

Change and extend the AppTest to assert:

```python
assert [tab.label for tab in app.tabs] == ["任务", "执行过程", "代码变更", "基准测试"]
assert app.title[0].value == "重构 Agent 运维仪表盘"
assert any(item.label == "管理员令牌" and item.proto.type == 1 for item in app.text_input)
assert any(item.label == "刷新数据" for item in app.button)
```

Add a `FakeDashboardApiClient` in `tests/test_dashboard.py` that returns one running job, one run, one trajectory, and fixed artifact text. Monkeypatch `refactor_agent.dashboard.DashboardApiClient` before creating `AppTest`, then assert the rendered labels `取消任务`, `重新执行`, `原始代码`, `候选代码`, and `代码差异`. Keep the existing unreachable-API AppTest and assert its Chinese empty-state/error context.

- [ ] **Step 2: Run AppTest and verify RED**

Run:

```powershell
pytest tests/test_dashboard.py::test_streamlit_dashboard_renders_four_operations_tabs -q
```

Expected: FAIL showing the current English tab, title, token, and button labels.

- [ ] **Step 3: Implement Chinese rendering**

Update `dashboard_main()` and tab renderers to use the approved terms:

```python
st.set_page_config(page_title="重构 Agent 运维仪表盘", layout="wide")
st.title("重构 Agent 运维仪表盘")
tasks_tab, execution_tab, code_tab, benchmark_tab = st.tabs(
    ["任务", "执行过程", "代码变更", "基准测试"]
)
```

Use Task 1 table helpers instead of `asdict()` or raw API dictionaries. Keep IDs and artifacts unchanged. Route every `DashboardApiError` through `format_dashboard_error()` before rendering.

- [ ] **Step 4: Run Dashboard tests and verify GREEN**

Run:

```powershell
pytest tests/test_dashboard.py -q
```

Expected: all Dashboard model, API-client, and AppTest cases pass.

---

### Task 3: 文档同步、运行验证与代码审查

**Files:**
- Modify: `README.md`
- Modify: `phase4-reliability-benchmark-dashboard-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-dashboard-chinese-localization.md`
- Review: `src/refactor_agent/dashboard.py`
- Review: `src/refactor_agent/dashboard_views.py`
- Review: `tests/test_dashboard.py`

**Interfaces:**
- Documents: fixed Simplified Chinese UI and preserved protocol values.
- Verifies: local API/Worker behavior remains unchanged and Streamlit remains reachable.

- [ ] **Step 1: Update documentation**

Replace English tab descriptions with `任务`、`执行过程`、`代码变更`、`基准测试`. Document that status text includes the original enum and that source/log content is not translated.

- [ ] **Step 2: Run focused and full verification**

Run:

```powershell
pytest tests/test_dashboard.py tests/test_webhook.py -q
pytest -q
python -m compileall -q src tests
git diff --check
```

Expected: all tests pass, compileall exits 0, and diff check emits no output.

- [ ] **Step 3: Restart the local Dashboard and inspect runtime health**

Stop only the Python process listening on `127.0.0.1:18501`, restart Streamlit from the current worktree with `REFACTOR_AGENT_API_URL=http://127.0.0.1:18000`, then verify:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18501/_stcore/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18000/health
```

Expected: both responses return HTTP 200.

- [ ] **Step 4: Perform full code review and self-fix**

Review all changed localization code for untranslated fixed text, raw-status business decisions, credential exposure, translated identifiers, inconsistent terms, AppTest gaps, and accidental changes outside Dashboard/docs. For each defect, add a failing regression test before fixing it, then rerun focused and full verification.

- [ ] **Step 5: Record evidence without committing**

Update this plan with test counts, runtime health results, review findings, and any unexecuted checks. Leave all changes uncommitted and unpushed for user review.

## Execution Record

Implementation and self-review completed on 2026-07-14 in the isolated `feat/reliability-benchmark-dashboard` worktree.

- View models: centralized Chinese status, failure-category, table-column, timeline, and API-error presentation while preserving raw identifiers and protocol values.
- Streamlit: localized the page title, sidebar, four tabs, metrics, selectors, controls, empty states, artifact headings, and common error context.
- TDD evidence: new localization tests failed first for missing helpers and English UI labels, then passed after minimal implementation; review findings for `Provider`, legacy status labels, and 404 context also followed RED/GREEN cycles.
- Focused verification: `pytest tests/test_dashboard.py tests/test_webhook.py -q` completed with 26 passed and one existing Starlette/httpx deprecation warning.
- Full verification: `pytest -q` completed with 181 passed and the same existing warning; `python -m compileall -q src tests` and `git diff --check` completed successfully.
- Runtime verification: Streamlit on `127.0.0.1:18501` and the Worker API on `127.0.0.1:18000` both returned HTTP 200 after restart.
- Security review: no credential patterns were found in the localization diff; Admin Token handling, raw-status control decisions, API contracts, Worker behavior, and Docker configuration remain unchanged.
- Integration state: no commit, push, merge, or deployment was performed.
