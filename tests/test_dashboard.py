from pathlib import Path
from datetime import datetime, timezone
import json

import httpx
import pytest

from refactor_agent import dashboard_views
from refactor_agent import dashboard as dashboard_module
from refactor_agent.dashboard_api import DashboardApiClient, DashboardApiError
from refactor_agent.dashboard_views import build_event_timeline, build_task_rows, job_actions
from refactor_agent.dashboard import (
    build_agent_chat_messages,
    build_before_after_rows,
    build_overview_chart_rows,
    load_dashboard_runs,
    load_trajectory,
)
from refactor_agent.models import RunRecord
from refactor_agent.store import SQLiteRunStore


def test_load_trajectory_handles_jsonl(tmp_path: Path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text(
        '{"attempt":1,"status":"MINIMIZER_PROPOSED","agent":"MINIMIZER","reward":{"reward":12.5}}\n',
        encoding="utf-8",
    )

    steps = load_trajectory(path)

    assert steps[0]["status"] == "MINIMIZER_PROPOSED"
    assert steps[0]["agent"] == "MINIMIZER"


def test_load_dashboard_runs_enriches_records(tmp_path: Path):
    run_root = tmp_path / ".runs"
    database = run_root / "runs.sqlite"
    store = SQLiteRunStore(database)
    store.save(
        RunRecord(
            run_id="20260101000000-demo",
            repo_name="demo",
            pre_loc=10,
            post_loc=2,
            pre_cc=5,
            post_cc=1,
            self_heal_count=1,
            status="SUCCESS",
        )
    )
    trajectory = run_root / "20260101000000-demo" / "trajectory.jsonl"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        '{"attempt":2,"status":"SUCCESS","reward":{"reward":27.0}}\n',
        encoding="utf-8",
    )

    runs = load_dashboard_runs(database, run_root)

    assert len(runs) == 1
    assert runs[0].loc_delta == -8
    assert runs[0].cc_delta == -4
    assert runs[0].reward == 27.0


def test_build_agent_chat_messages_prepares_live_stream():
    messages = build_agent_chat_messages(
        [
            {
                "attempt": 1,
                "status": "MINIMIZER_PROPOSED",
                "agent": "MINIMIZER",
                "message": "try tiny code",
            },
            {
                "attempt": 1,
                "status": "JUDGE_SCORED",
                "agent": "JUDGE",
                "message": "score it",
                "reward": {"reward": 12.5},
            },
        ]
    )

    assert len(messages) == 2
    assert messages[0].agent == "MINIMIZER"
    assert messages[0].side == "left"
    assert messages[0].tone == "minimizer"
    assert messages[1].side == "center"
    assert messages[1].reward == 12.5


def test_build_dashboard_chart_rows(tmp_path: Path):
    run_root = tmp_path / ".runs"
    database = run_root / "runs.sqlite"
    store = SQLiteRunStore(database)
    store.save(
        RunRecord(
            run_id="20260101000000-demo",
            repo_name="demo",
            pre_loc=20,
            post_loc=8,
            pre_cc=9,
            post_cc=3,
            self_heal_count=0,
            status="SUCCESS",
        )
    )
    runs = load_dashboard_runs(database, run_root)

    assert build_overview_chart_rows(runs) == [
        {"运行": "000-demo", "LOC 变化": -12, "CC 变化": -6, "奖励分": 0}
    ]
    assert build_before_after_rows(runs[0]) == [
        {"指标": "LOC", "重构前": 20, "重构后": 8},
        {"指标": "CC", "重构前": 9, "重构后": 3},
    ]


def test_dashboard_api_sends_admin_token_only_for_control_requests():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "job-1", "status": "CANCEL_REQUESTED"})
        return httpx.Response(200, json={"jobs": []})

    client = DashboardApiClient(
        "http://testserver",
        admin_token="admin-secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.list_jobs() == []
    client.cancel_job("job-1")

    assert "Authorization" not in requests[0].headers
    assert requests[1].headers["Authorization"] == "Bearer admin-secret"
    assert "admin-secret" not in str(requests[1].url)


def test_dashboard_api_surfaces_control_conflicts():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(409, json={"detail": "terminal job"})
    )
    client = DashboardApiClient("http://testserver", admin_token="token", transport=transport)

    try:
        client.retry_job("job-1")
    except DashboardApiError as exc:
        assert exc.status_code == 409
        assert "terminal job" in str(exc)
    else:
        raise AssertionError("expected DashboardApiError")


def test_dashboard_api_submits_url_job_with_admin_header_only():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/capabilities":
            return httpx.Response(
                200,
                json={
                    "sandbox_backend": "docker",
                    "graph_backend": "langgraph",
                    "llm_mode": "deepseek",
                    "url_submission": True,
                },
            )
        return httpx.Response(202, json={"job_id": "url-job-1", "status": "QUEUED"})

    client = DashboardApiClient(
        "http://testserver",
        admin_token="admin-secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_capabilities()["llm_mode"] == "deepseek"
    result = client.submit_url_job(
        repository_url="https://github.com/octo/demo",
        refactor_request="简化 calculate 函数",
        branch=None,
        target_path=None,
        tests_path="tests",
    )

    assert result["job_id"] == "url-job-1"
    assert "Authorization" not in requests[0].headers
    assert requests[1].headers["Authorization"] == "Bearer admin-secret"
    assert json.loads(requests[1].content)["repository_url"] == "https://github.com/octo/demo"
    assert "admin-secret" not in requests[1].content.decode("utf-8")


def test_dashboard_api_manages_repository_allowlist_with_admin_header():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"entries": [{"repo_full_name": "octo/demo", "source": "DASHBOARD"}]},
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"repo_full_name": "octo/demo", "source": "DASHBOARD"},
            )
        return httpx.Response(200, json={"repo_full_name": "octo/demo", "removed": True})

    client = DashboardApiClient(
        "http://testserver",
        admin_token="admin-secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.list_repository_allowlist()[0]["repo_full_name"] == "octo/demo"
    assert client.add_repository_allowlist("https://github.com/octo/demo")["source"] == "DASHBOARD"
    assert client.remove_repository_allowlist("octo/demo")["removed"] is True

    assert [request.method for request in requests] == ["GET", "POST", "DELETE"]
    assert all(request.headers["Authorization"] == "Bearer admin-secret" for request in requests)
    assert requests[-1].url.path == "/admin/repository-allowlist/octo/demo"


def test_dashboard_status_labels_keep_raw_values():
    assert dashboard_views.localize_status("RUNNING") == "运行中（RUNNING）"
    assert (
        dashboard_views.localize_status("MINIMIZER_PROPOSED")
        == "精简者已提交方案（MINIMIZER_PROPOSED）"
    )
    assert dashboard_views.localize_status("CUSTOM") == "未知状态（CUSTOM）"


def test_dashboard_tables_use_chinese_columns_without_translating_identifiers():
    rows = build_task_rows(
        [{"job_id": "job-1", "job_kind": "DASHBOARD_URL", "status": "RUNNING"}]
    )
    task_table = dashboard_views.build_task_table_rows(rows)
    assert task_table[0]["任务 ID"] == "job-1"
    assert task_table[0]["来源"] == "仪表盘 URL"
    assert task_table[0]["状态"] == "运行中（RUNNING）"
    assert "can_cancel" not in task_table[0]

    timeline = build_event_timeline(
        [
            {
                "event_id": "event-1",
                "from_status": "QUEUED",
                "to_status": "RUNNING",
                "message": "claimed",
            }
        ]
    )
    assert dashboard_views.build_timeline_rows(timeline) == [
        {
            "事件 ID": "event-1",
            "时间": "",
            "状态变化": "排队中（QUEUED） -> 运行中（RUNNING）",
            "消息": "claimed",
            "Worker ID": None,
        }
    ]

    execution = dashboard_views.build_execution_rows(
        [{"attempt": 1, "agent": "JUDGE", "status": "SUCCESS", "message": "approved"}]
    )
    assert set(execution[0]) == {"轮次", "Agent", "状态", "消息", "奖励分"}
    assert execution[0]["状态"] == "成功（SUCCESS）"
    assert execution[0]["消息"] == "approved"

    benchmark_runs = dashboard_views.build_benchmark_run_rows(
        [
            {
                "run_id": "bench-1",
                "manifest_hash": "abc123",
                "provider": "mock",
                "model": "deterministic",
                "status": "SUCCESS",
                "generated_at": "2026-07-14T00:00:00Z",
            }
        ]
    )
    assert benchmark_runs[0]["运行 ID"] == "bench-1"
    assert benchmark_runs[0]["服务提供方"] == "mock"
    assert "Provider" not in benchmark_runs[0]
    assert benchmark_runs[0]["状态"] == "成功（SUCCESS）"

    benchmark_cases = dashboard_views.build_benchmark_rows(
        [
            {
                "case_name": "case-1",
                "repository": "owner/repo",
                "status": "FAILED",
                "failure_category": "PYTEST",
                "total_tokens": 4,
                "cost_usd": 0.1,
                "attempts": 2,
            }
        ]
    )
    assert benchmark_cases[0]["案例"] == "case-1"
    assert benchmark_cases[0]["状态"] == "失败（FAILED）"
    assert benchmark_cases[0]["失败类别"] == "测试失败（PYTEST）"


def test_dashboard_errors_add_chinese_context_and_keep_detail():
    assert dashboard_views.format_dashboard_error(400, "invalid ref") == (
        "提交内容格式错误。详细信息：invalid ref"
    )
    assert dashboard_views.format_dashboard_error(401, "invalid token") == (
        "管理员令牌无效或缺失。详细信息：invalid token"
    )
    assert dashboard_views.format_dashboard_error(404, "run not found") == (
        "请求的任务、运行记录或产物不存在。详细信息：run not found"
    )
    assert dashboard_views.format_dashboard_error(409, "terminal job") == (
        "当前状态不允许执行该操作。详细信息：terminal job"
    )
    assert dashboard_views.format_dashboard_error(503, "docker unavailable") == (
        "Worker 当前无法接受 URL 任务。详细信息：docker unavailable"
    )
    assert dashboard_views.format_dashboard_error(None, "connection refused") == (
        "无法连接本地 API。详细信息：connection refused"
    )


def test_legacy_arena_uses_the_shared_status_label():
    assert dashboard_module._status_label("SUCCESS") == "成功（SUCCESS）"
    assert dashboard_module._status_label("CUSTOM") == "未知状态（CUSTOM）"


def test_dashboard_view_models_build_actions_and_event_timeline():
    jobs = [
        {
            "job_id": "queued",
            "status": "QUEUED",
            "attempt_count": 0,
            "pr_url": None,
            "deadline_at": "2026-07-14T00:01:00+00:00",
        },
        {"job_id": "failed", "status": "FAILED", "attempt_count": 2, "issue_number": 42, "pr_url": None},
        {"job_id": "pr", "status": "FAILED", "attempt_count": 1, "pr_url": "https://example/pull/1"},
    ]

    rows = build_task_rows(jobs, now=datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert rows[0].can_cancel is True
    assert rows[0].can_retry is False
    assert rows[0].remaining_seconds == 60
    assert rows[1].can_cancel is False
    assert rows[1].can_retry is True
    assert rows[1].attempts == 2
    assert rows[1].issue_number == 42
    assert job_actions("FAILED", "https://example/pull/1") == (False, False)

    timeline = build_event_timeline(
        [
            {"event_id": "1", "to_status": "QUEUED", "created_at": "2026-07-14T00:00:00Z"},
            {"event_id": "2", "from_status": "QUEUED", "to_status": "RUNNING", "created_at": "2026-07-14T00:00:01Z"},
        ]
    )
    assert [item.label for item in timeline] == ["QUEUED", "QUEUED -> RUNNING"]


def test_streamlit_dashboard_renders_four_operations_tabs(monkeypatch):
    streamlit = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("REFACTOR_AGENT_API_URL", "http://127.0.0.1:1")

    app = streamlit.AppTest.from_file("tests/streamlit_dashboard_app.py").run(timeout=10)

    assert [tab.label for tab in app.tabs] == ["任务", "执行过程", "代码变更", "基准测试"]
    assert app.title[0].value == "重构 Agent 运维仪表盘"
    admin_inputs = [item for item in app.text_input if item.label == "管理员令牌"]
    assert len(admin_inputs) == 1
    assert admin_inputs[0].proto.type == 1
    assert any(item.label == "刷新数据" for item in app.button)
    assert any("无法连接本地 API" in item.value for item in app.error)
    assert any("模型：未知" in item.value for item in app.caption)
    assert {item.value for item in app.info} >= {
        "暂无任务。",
        "暂无执行记录。",
        "暂无代码产物。",
        "暂无基准测试记录。",
    }


def test_streamlit_dashboard_renders_chinese_controls_and_artifact_sections(monkeypatch):
    streamlit = pytest.importorskip("streamlit.testing.v1")

    class FakeDashboardApiClient:
        submissions: list[dict[str, object]] = []
        allowlist_actions: list[tuple[str, str]] = []
        allowlist_reads = 0

        def __init__(self, api_url: str, admin_token: str | None = None, timeout_seconds: float = 3.0):
            self.api_url = api_url
            self.admin_token = admin_token

        def list_jobs(self, limit: int = 100):
            return [
                {
                    "job_id": "job-1",
                    "job_kind": "DASHBOARD_URL",
                    "status": "RUNNING",
                    "repo_full_name": "owner/repo",
                    "issue_number": 7,
                    "attempt_count": 1,
                    "lease_owner": "worker-1",
                    "run_id": "run-1",
                }
            ]

        def get_capabilities(self):
            return {
                "sandbox_backend": "docker",
                "graph_backend": "langgraph",
                "llm_mode": "deepseek",
                "url_submission": True,
            }

        def submit_url_job(self, **payload):
            self.submissions.append(payload)
            return {"job_id": "url-job-created", "status": "QUEUED"}

        def list_repository_allowlist(self):
            type(self).allowlist_reads += 1
            return [
                {
                    "repo_full_name": "owner/environment",
                    "source": "ENVIRONMENT",
                    "removable": False,
                    "created_at": None,
                },
                {
                    "repo_full_name": "owner/dashboard",
                    "source": "DASHBOARD",
                    "removable": True,
                    "created_at": "2026-07-15T00:00:00+00:00",
                },
            ]

        def add_repository_allowlist(self, repository: str):
            self.allowlist_actions.append(("ADD", repository))
            return {"repo_full_name": repository.lower(), "source": "DASHBOARD"}

        def remove_repository_allowlist(self, repository: str):
            self.allowlist_actions.append(("REMOVE", repository))
            return {"repo_full_name": repository, "removed": True}

        def list_runs(self, limit: int = 100):
            return [{"run_id": "run-1"}]

        def list_benchmarks(self, limit: int = 20):
            return [
                {
                    "run_id": "bench-1",
                    "manifest_hash": "abc123",
                    "provider": "mock",
                    "model": "deterministic",
                    "status": "SUCCESS",
                    "generated_at": "2026-07-14T00:00:00Z",
                }
            ]

        def list_events(self, job_id: str):
            return [{"event_id": "event-1", "to_status": "RUNNING", "message": "claimed"}]

        def get_trajectory(self, run_id: str):
            return [{"attempt": 1, "agent": "JUDGE", "status": "SUCCESS", "message": "ok"}]

        def get_artifact(self, run_id: str, artifact_name: str):
            return {
                "original.py": "def value():\n    return 1\n",
                "candidate.py": "def value():\n    return 2\n",
                "change.diff": "-    return 1\n+    return 2\n",
                "pytest.log": "1 passed",
                "adversary.log": "passed",
            }[artifact_name]

        def get_benchmark(self, run_id: str):
            return {"cases": [{"case_name": "case-1", "status": "SUCCESS"}]}

        def cancel_job(self, job_id: str):
            return {"job_id": job_id, "status": "CANCEL_REQUESTED"}

        def retry_job(self, job_id: str):
            return {"job_id": job_id, "status": "QUEUED"}

    monkeypatch.setattr(dashboard_module, "DashboardApiClient", FakeDashboardApiClient)
    monkeypatch.setenv("REFACTOR_AGENT_API_URL", "http://testserver")

    app = streamlit.AppTest.from_file("tests/streamlit_dashboard_app.py").run(timeout=10)

    button_labels = {item.label for item in app.button}
    assert {"刷新数据", "取消任务", "重新执行"} <= button_labels
    assert {item.value for item in app.subheader} >= {"原始代码", "候选代码", "代码差异"}
    assert any(item.label == "选择任务" for item in app.selectbox)
    assert any(item.label == "选择执行记录" for item in app.selectbox)
    assert any(item.label == "选择代码运行记录" for item in app.selectbox)
    assert any(item.label == "选择基准测试记录" for item in app.selectbox)
    assert any(item.label == "GitHub 仓库 URL" for item in app.text_input)
    assert any(item.label == "分支或标签（可选）" for item in app.text_input)
    assert any(item.label == "目标文件（可选）" for item in app.text_input)
    assert any(item.label == "测试路径" for item in app.text_input)
    assert any(item.label == "简化要求" for item in app.text_area)
    assert any(item.label == "创建本地简化任务" for item in app.button)
    assert any("真实 DeepSeek" in item.value for item in app.caption)

    initial_submit = next(item for item in app.button if item.label == "创建本地简化任务")
    assert initial_submit.disabled is True
    assert FakeDashboardApiClient.allowlist_reads == 0
    next(item for item in app.text_input if item.label == "管理员令牌").set_value("admin-secret")
    app.run(timeout=10)
    enabled_submit = next(item for item in app.button if item.label == "创建本地简化任务")
    assert enabled_submit.disabled is False
    assert any(item.label == "仓库名称或 URL" for item in app.text_input)
    assert any(item.label == "添加仓库" for item in app.button)
    assert any(item.label == "选择要移除的仓库" for item in app.selectbox)
    assert any(item.label == "移除仓库" for item in app.button)
    assert FakeDashboardApiClient.allowlist_reads >= 1

    next(item for item in app.text_input if item.label == "仓库名称或 URL").set_value(
        "owner/new-repo"
    )
    app.run(timeout=10)
    next(item for item in app.button if item.label == "添加仓库").click().run(timeout=10)
    assert ("ADD", "owner/new-repo") in FakeDashboardApiClient.allowlist_actions
    assert any("owner/new-repo" in item.value for item in app.success)

    next(item for item in app.button if item.label == "移除仓库").click().run(timeout=10)
    assert ("REMOVE", "owner/dashboard") in FakeDashboardApiClient.allowlist_actions
    assert any("owner/dashboard" in item.value for item in app.success)

    enabled_submit = next(item for item in app.button if item.label == "创建本地简化任务")
    enabled_submit.click().run(timeout=10)
    assert FakeDashboardApiClient.submissions == []
    assert any("请填写仓库 URL、测试路径和简化要求" in item.value for item in app.error)

    next(item for item in app.text_input if item.label == "GitHub 仓库 URL").set_value(
        "https://github.com/octo/demo"
    )
    next(item for item in app.text_area if item.label == "简化要求").set_value(
        "简化 calculate 函数"
    )
    app.run(timeout=10)
    submit = next(item for item in app.button if item.label == "创建本地简化任务")
    assert submit.disabled is False

    submit.click().run(timeout=10)

    assert FakeDashboardApiClient.submissions[-1] == {
        "repository_url": "https://github.com/octo/demo",
        "refactor_request": "简化 calculate 函数",
        "branch": None,
        "target_path": None,
        "tests_path": "tests",
    }
    assert any("url-job-created" in item.value for item in app.success)
