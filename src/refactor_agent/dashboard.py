from __future__ import annotations

from html import escape
import os
from pathlib import Path
from typing import Any

from refactor_agent.debate_state import render_mermaid_state_diagram
from refactor_agent.dashboard_api import DashboardApiClient, DashboardApiError
from refactor_agent.dashboard_views import (
    DashboardChatMessage,
    DashboardRun,
    _agent_label,
    _average,
    _average_float,
    _candidate_files,
    _delta,
    _format_delta,
    _format_float,
    _format_percent,
    _last_reward,
    _metadata_summary,
    _phase_label,
    _record_summary,
    _reduction_percent,
    _status_label,
    _table_row,
    build_agent_chat_messages,
    build_benchmark_run_rows,
    build_benchmark_rows,
    build_before_after_rows,
    build_event_timeline,
    build_execution_rows,
    build_task_rows,
    build_task_table_rows,
    build_timeline_rows,
    format_dashboard_error,
    load_trajectory,
    localize_job_status,
)
from refactor_agent.persona import extract_persona_markdown
from refactor_agent.store import SQLiteRunStore


def load_dashboard_runs(database_path: Path, run_root: Path, limit: int = 20) -> list[DashboardRun]:
    store = SQLiteRunStore(database_path)
    rows: list[DashboardRun] = []
    for record in store.list_runs(limit):
        trajectory = load_trajectory(run_root / record.run_id / "trajectory.jsonl")
        rows.append(
            DashboardRun(
                record=record,
                workspace_path=run_root / record.run_id / "workspace",
                loc_delta=_delta(record.pre_loc, record.post_loc),
                cc_delta=_delta(record.pre_cc, record.post_cc),
                loc_reduction_percent=_reduction_percent(record.pre_loc, record.post_loc),
                cc_reduction_percent=_reduction_percent(record.pre_cc, record.post_cc),
                reward=_last_reward(trajectory),
                trajectory=trajectory,
                candidate_files=_candidate_files(run_root / record.run_id / "workspace"),
            )
        )
    return rows


def dashboard_main() -> None:
    import streamlit as st

    st.set_page_config(page_title="代码审判助手", layout="wide")
    st.title("代码审判助手")

    with st.sidebar:
        api_url = st.text_input(
            "API 地址",
            value=os.getenv("REFACTOR_AGENT_API_URL", "http://127.0.0.1:8000"),
        )
        admin_token = ""
        limit = st.number_input("记录数量上限", min_value=5, max_value=100, value=50, step=5)
        if st.button("刷新数据", width="stretch"):
            st.rerun()

    client = DashboardApiClient(api_url, admin_token=admin_token or None, timeout_seconds=1.0)
    jobs: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    benchmarks: list[dict[str, Any]] = []
    capabilities: dict[str, Any] = {}
    try:
        capabilities = client.get_capabilities()
        jobs = client.list_jobs(int(limit))
        runs = client.list_runs(int(limit))
        benchmarks = client.list_benchmarks(int(limit))
    except DashboardApiError as exc:
        _show_dashboard_error(st, exc)

    if capabilities.get("admin_token_required"):
        with st.sidebar:
            admin_token = st.text_input("管理员令牌", type="password", value="")
        client = DashboardApiClient(api_url, admin_token=admin_token or None, timeout_seconds=1.0)

    tasks_tab, execution_tab, code_tab, benchmark_tab = st.tabs(
        ["任务", "执行过程", "代码变更", "基准测试"]
    )
    with tasks_tab:
        _render_tasks_tab(
            st,
            client,
            jobs,
            not capabilities.get("admin_token_required") or bool(admin_token),
            capabilities,
        )
    with execution_tab:
        _render_execution_tab(st, client, jobs, runs)
    with code_tab:
        _render_code_tab(st, client, jobs, runs)
    with benchmark_tab:
        _render_benchmarks_tab(st, client, benchmarks)


def _render_tasks_tab(
    st,
    client: DashboardApiClient,
    jobs: list[dict[str, Any]],
    admin_enabled: bool,
    capabilities: dict[str, Any],
) -> None:
    _render_snippet_submission_form(st, client, admin_enabled, capabilities)
    _render_url_submission_form(st, client, admin_enabled, capabilities)
    _render_repository_allowlist_manager(st, client, admin_enabled)
    rows = build_task_rows(jobs)
    if not rows:
        st.info("暂无任务。")
        return
    st.dataframe(build_task_table_rows(rows), width="stretch", hide_index=True)
    selected_id = st.selectbox("选择任务", [row.job_id for row in rows], key="tasks_job")
    selected = next(row for row in rows if row.job_id == selected_id)
    raw = next(job for job in jobs if str(job.get("job_id")) == selected_id)
    metrics = st.columns(6)
    metrics[0].metric("状态", localize_job_status(selected.status, selected.job_kind))
    metrics[1].metric("尝试次数", selected.attempts)
    metrics[2].metric("租约所有者", selected.lease_owner or "-")
    metrics[3].metric("截止时间", str(raw.get("deadline_at") or "-"))
    metrics[4].metric(
        "剩余时间",
        f"{selected.remaining_seconds} 秒" if selected.remaining_seconds is not None else "-",
    )
    metrics[5].metric("运行 ID", str(raw.get("run_id") or "-"))
    try:
        timeline = build_event_timeline(client.list_events(selected_id))
        st.dataframe(build_timeline_rows(timeline), width="stretch", hide_index=True)
    except DashboardApiError as exc:
        _show_dashboard_error(st, exc)
    controls = st.columns(2)
    if controls[0].button(
        "取消任务",
        disabled=not (admin_enabled and selected.can_cancel),
        width="stretch",
    ):
        _run_control(st, lambda: client.cancel_job(selected_id))
    if controls[1].button(
        "重新执行",
        disabled=not (admin_enabled and selected.can_retry),
        width="stretch",
    ):
        _run_control(st, lambda: client.retry_job(selected_id))


def _render_repository_allowlist_manager(
    st,
    client: DashboardApiClient,
    admin_enabled: bool,
) -> None:
    success_message = st.session_state.pop("allowlist_success", None)
    if success_message:
        st.success(success_message)
    with st.expander("仓库白名单"):
        if not admin_enabled:
            st.info("填写管理员令牌后才能查看和管理仓库白名单。")
            return
        try:
            entries = client.list_repository_allowlist()
        except DashboardApiError as exc:
            _show_dashboard_error(st, exc)
            return

        if entries:
            st.dataframe(
                [
                    {
                        "仓库": entry.get("repo_full_name"),
                        "来源": (
                            "环境变量"
                            if entry.get("source") == "ENVIRONMENT"
                            else "仪表盘"
                        ),
                        "添加时间": entry.get("created_at") or "-",
                        "可移除": "是" if entry.get("removable") else "否",
                    }
                    for entry in entries
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.warning("当前白名单为空，所有仓库任务都会被拒绝。")

        with st.form("allowlist_add_form", clear_on_submit=False):
            repository = st.text_input(
                "仓库名称或 URL",
                placeholder="owner/repository 或 https://github.com/owner/repository",
            )
            add_submitted = st.form_submit_button("添加仓库", width="stretch")
        if add_submitted:
            if not repository.strip():
                st.error("请输入仓库名称或 GitHub URL。")
            else:
                try:
                    result = client.add_repository_allowlist(repository.strip())
                except DashboardApiError as exc:
                    _show_dashboard_error(st, exc)
                else:
                    st.session_state["allowlist_success"] = (
                        f"仓库已加入白名单：{result.get('repo_full_name', repository.strip())}"
                    )
                    st.rerun()

        removable = [
            str(entry.get("repo_full_name"))
            for entry in entries
            if entry.get("removable") and entry.get("repo_full_name")
        ]
        selected_repository = st.selectbox(
            "选择要移除的仓库",
            removable,
            disabled=not removable,
        )
        if st.button("移除仓库", disabled=not removable, width="stretch"):
            try:
                result = client.remove_repository_allowlist(str(selected_repository))
            except DashboardApiError as exc:
                _show_dashboard_error(st, exc)
            else:
                removed = bool(result.get("removed"))
                st.session_state["allowlist_success"] = (
                    f"仓库已移出白名单：{selected_repository}"
                    if removed
                    else f"仓库原本不在仪表盘白名单中：{selected_repository}"
                )
                st.rerun()


def _render_url_submission_form(
    st,
    client: DashboardApiClient,
    admin_enabled: bool,
    capabilities: dict[str, Any],
) -> None:
    success_job_id = st.session_state.pop("url_submission_success", None)
    if success_job_id:
        st.success(f"本地简化任务已创建：{success_job_id}")
    llm_mode = capabilities.get("llm_mode")
    if llm_mode == "deepseek":
        llm_label = "真实 DeepSeek"
    elif llm_mode == "mock":
        llm_label = "本地 Mock"
    else:
        llm_label = "未知"
    sandbox = str(capabilities.get("sandbox_backend") or "-")
    graph = str(capabilities.get("graph_backend") or "-")
    submission_enabled = bool(capabilities.get("url_submission"))
    with st.expander("从 GitHub URL 创建本地简化任务"):
        st.caption(f"模型：{llm_label} | 沙箱：{sandbox} | 图后端：{graph} | 结果仅保存在本地")
        if not admin_enabled:
            st.info("填写管理员令牌后才能创建任务。")
        if not submission_enabled:
            st.warning("Worker 当前未启用 Docker 或可用模型，暂时不能提交 URL 任务。")
        with st.form("url_job_form", clear_on_submit=False):
            repository_url = st.text_input("GitHub 仓库 URL", placeholder="https://github.com/owner/repo")
            branch = st.text_input("分支或标签（可选）", placeholder="留空时使用默认分支")
            target_path = st.text_input("目标文件（可选）", placeholder="留空时自动定位")
            tests_path = st.text_input("测试路径", value="tests")
            persona_label = st.segmented_control(
                "报告人格",
                ["严格评审", "傲娇审判"],
                default="严格评审",
                key="url_persona",
            )
            refactor_request = st.text_area("简化要求", height=150)
            submitted = st.form_submit_button(
                "创建本地简化任务",
                disabled=not admin_enabled or not submission_enabled,
                width="stretch",
            )
        if submitted:
            if not repository_url.strip() or not tests_path.strip() or not refactor_request.strip():
                st.error("请填写仓库 URL、测试路径和简化要求。")
                return
            try:
                result = client.submit_url_job(
                    repository_url=repository_url.strip(),
                    refactor_request=refactor_request.strip(),
                    branch=branch.strip() or None,
                    target_path=target_path.strip() or None,
                    tests_path=tests_path.strip(),
                    persona="TSUNDERE" if persona_label == "傲娇审判" else "STRICT",
                )
            except DashboardApiError as exc:
                _show_dashboard_error(st, exc)
            else:
                job_id = str(result.get("job_id") or "")
                st.session_state["url_submission_success"] = job_id
                st.session_state["tasks_job"] = job_id
                st.rerun()


def _render_snippet_submission_form(
    st,
    client: DashboardApiClient,
    admin_enabled: bool,
    capabilities: dict[str, Any],
) -> None:
    success_job_id = st.session_state.pop("snippet_submission_success", None)
    if success_job_id:
        st.success(f"代码审判任务已创建：{success_job_id}")
    enabled = bool(capabilities.get("snippet_submission"))
    product_mode = str(capabilities.get("product_mode") or "demo")
    with st.expander("粘贴 Python 代码进行审判", expanded=True):
        if product_mode == "demo":
            st.warning("当前是演示模式，只支持内置函数模式；任意代码需要配置 DeepSeek。")
        else:
            st.caption("当前使用真实 DeepSeek；代码和测试会保存在本地任务与运行产物中。")
        if not admin_enabled:
            st.info("填写管理员令牌后才能创建任务。")
        if not enabled:
            st.warning("Worker 当前未启用 Docker 或可用模型，暂时不能提交代码任务。")
        with st.form("snippet_job_form", clear_on_submit=False):
            mode_label = st.segmented_control(
                "模式",
                ["自动生成测试", "使用我的测试"],
                default="自动生成测试",
            )
            persona_label = st.segmented_control(
                "报告人格",
                ["严格评审", "傲娇审判"],
                default="严格评审",
            )
            source = st.text_area("Python 源码", height=260, placeholder="def example():\n    pass")
            requirement = st.text_area("审查或精简要求", height=100)
            tests = st.text_area(
                "Pytest 测试源码（使用我的测试时必填）",
                height=180,
                placeholder="固定使用 from snippet import example\n\ndef test_example():\n    ...",
            )
            submitted = st.form_submit_button(
                "创建代码审判任务",
                disabled=not admin_enabled or not enabled,
                width="stretch",
            )
        if submitted:
            mode = "VERIFIED_REFACTOR" if mode_label == "使用我的测试" else "REVIEW"
            persona = "TSUNDERE" if persona_label == "傲娇审判" else "STRICT"
            if not source.strip() or not requirement.strip():
                st.error("请填写 Python 源码和审查要求。")
                return
            if mode == "VERIFIED_REFACTOR" and not tests.strip():
                st.error("使用我的测试模式必须填写 pytest 测试源码。")
                return
            try:
                result = client.submit_snippet_job(
                    source=source,
                    refactor_request=requirement.strip(),
                    tests=tests or None,
                    mode=mode,
                    persona=persona,
                )
            except DashboardApiError as exc:
                _show_dashboard_error(st, exc)
            else:
                job_id = str(result.get("job_id") or "")
                st.session_state["snippet_submission_success"] = job_id
                st.session_state["tasks_job"] = job_id
                st.rerun()


def _render_execution_tab(
    st,
    client: DashboardApiClient,
    jobs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    run_ids = _available_run_ids(jobs, runs)
    if not run_ids:
        st.info("暂无执行记录。")
        return
    run_id = st.selectbox("选择执行记录", run_ids, key="execution_run")
    try:
        selected_run = next((run for run in runs if run.get("run_id") == run_id), {})
        evidence = str(selected_run.get("evidence_level") or "-")
        persona = str(selected_run.get("report_persona") or "-")
        st.caption(f"证据等级：{evidence} | 报告人格：{persona}")
        trajectory = client.get_trajectory(run_id)
        st.dataframe(build_execution_rows(trajectory), width="stretch", hide_index=True)
        left, right = st.columns(2)
        left.text_area("Pytest 日志", client.get_artifact(run_id, "pytest.log"), height=260, disabled=True)
        right.text_area("对抗测试日志", client.get_artifact(run_id, "adversary.log"), height=260, disabled=True)
        st.subheader("人格化审判")
        st.markdown(extract_persona_markdown(client.get_artifact(run_id, "report.md")))
    except DashboardApiError as exc:
        _show_dashboard_error(st, exc)


def _render_code_tab(
    st,
    client: DashboardApiClient,
    jobs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    run_ids = _available_run_ids(jobs, runs)
    if not run_ids:
        st.info("暂无代码产物。")
        return
    run_id = st.selectbox("选择代码运行记录", run_ids, key="code_run")
    try:
        original = client.get_artifact(run_id, "original.py")
        candidate = client.get_artifact(run_id, "candidate.py")
        diff = client.get_artifact(run_id, "change.diff")
        before, after = st.columns(2)
        before.subheader("原始代码")
        before.code(original, language="python", line_numbers=True)
        after.subheader("候选代码")
        after.code(candidate, language="python", line_numbers=True)
        st.subheader("代码差异")
        st.code(diff, language="diff", line_numbers=True)
    except DashboardApiError as exc:
        _show_dashboard_error(st, exc)


def _render_benchmarks_tab(st, client: DashboardApiClient, benchmarks: list[dict[str, Any]]) -> None:
    if not benchmarks:
        st.info("暂无基准测试记录。")
        return
    st.dataframe(build_benchmark_run_rows(benchmarks), width="stretch", hide_index=True)
    run_id = st.selectbox(
        "选择基准测试记录",
        [str(item.get("run_id")) for item in benchmarks],
        key="benchmark_run",
    )
    try:
        detail = client.get_benchmark(run_id)
        st.dataframe(build_benchmark_rows(detail.get("cases", [])), width="stretch", hide_index=True)
    except DashboardApiError as exc:
        _show_dashboard_error(st, exc)


def _available_run_ids(jobs: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[str]:
    values = {
        str(value)
        for value in [
            *(job.get("run_id") for job in jobs),
            *(run.get("run_id") for run in runs),
        ]
        if value
    }
    return sorted(values, reverse=True)


def _run_control(st, action) -> None:
    try:
        action()
    except DashboardApiError as exc:
        _show_dashboard_error(st, exc)
        return
    st.rerun()


def _show_dashboard_error(st, error: DashboardApiError) -> None:
    st.error(format_dashboard_error(error.status_code, str(error)))


def render_dashboard(database_path: Path, run_root: Path) -> None:
    import streamlit as st

    st.set_page_config(page_title="重构 Agent 竞技场", layout="wide")
    st.title("重构 Agent 竞技场")

    with st.sidebar:
        st.header("控制台")
        st.write(f"数据库：`{database_path}`")
        st.write(f"运行目录：`{run_root}`")
        limit = st.slider("读取最近运行数", min_value=5, max_value=100, value=30, step=5)
        status_filter = st.multiselect("状态过滤", ["SUCCESS", "FAILED"], default=["SUCCESS", "FAILED"])
        st.divider()
        st.subheader("竞技场命令")
        st.code(
            "refactor-agent demo-cases\n"
            "refactor-agent demo-suite --sandbox-backend auto\n"
            "refactor-agent demo --case add-maze --sandbox-backend auto\n"
            "refactor-agent dashboard --host 127.0.0.1 --port 8501",
            language="powershell",
        )
        if st.button("刷新数据"):
            st.rerun()

    runs = [
        item
        for item in load_dashboard_runs(database_path, run_root, limit=limit)
        if item.record.status in status_filter
    ]
    if not runs:
        st.info("暂无运行记录。先执行 `refactor-agent demo-suite --sandbox-backend auto`。")
        return

    successes = sum(1 for item in runs if item.record.status == "SUCCESS")
    avg_loc = _average([item.loc_delta for item in runs if item.loc_delta is not None])
    avg_cc = _average([item.cc_delta for item in runs if item.cc_delta is not None])
    avg_reward = _average_float([item.reward for item in runs if item.reward is not None])
    avg_retry = _average([item.record.self_heal_count for item in runs])

    metric_columns = st.columns(6)
    metric_columns[0].metric("运行次数", len(runs))
    metric_columns[1].metric("成功率", f"{successes / len(runs) * 100:.0f}%")
    metric_columns[2].metric("平均自愈轮次", _format_float(avg_retry))
    metric_columns[3].metric("平均 LOC 变化", _format_delta(avg_loc))
    metric_columns[4].metric("平均 CC 变化", _format_delta(avg_cc))
    metric_columns[5].metric("平均奖励分", _format_float(avg_reward))

    tab_live, tab_overview, tab_detail, tab_trajectory = st.tabs(
        ["竞技场", "战况", "代码", "轨迹"]
    )

    with tab_live:
        active = _select_run(st, runs, key="live_run")
        _render_live_room(st, active)

    with tab_overview:
        _render_overview(st, runs)

    with tab_detail:
        active = _select_run(st, runs)
        _render_run_detail(st, active)

    with tab_trajectory:
        active = _select_run(st, runs, key="trajectory_run")
        _render_trajectory(st, active)


def _render_live_room(st, active: DashboardRun) -> None:
    st.subheader("实时竞技场")
    left, right = st.columns([1.2, 1])

    with left:
        st.markdown("##### 攻防回合")
        _render_chat_stream(st, build_agent_chat_messages(active.trajectory))

    with right:
        st.markdown("##### 裁决面板")
        columns = st.columns(4)
        columns[0].metric("状态", _status_label(active.record.status))
        columns[1].metric("自愈轮次", active.record.self_heal_count)
        columns[2].metric("LOC 变化", _format_delta(active.loc_delta))
        columns[3].metric("奖励分", _format_float(active.reward))
        st.bar_chart(build_before_after_rows(active), x="指标", y=["重构前", "重构后"])
        st.markdown("##### 状态机")
        st.code(render_mermaid_state_diagram(), language="mermaid")


def _render_chat_stream(st, messages: list[DashboardChatMessage]) -> None:
    if not messages:
        st.info("这次运行还没有可展示的 Agent 发言。")
        return

    st.markdown(
        """
<style>
.agent-chat-stream { display: flex; flex-direction: column; gap: 0.65rem; }
.agent-chat-row { display: flex; }
.agent-chat-row.left { justify-content: flex-start; }
.agent-chat-row.right { justify-content: flex-end; }
.agent-chat-row.center { justify-content: center; }
.agent-bubble {
  max-width: 92%;
  border: 1px solid rgba(49, 51, 63, 0.16);
  border-radius: 8px;
  padding: 0.7rem 0.85rem;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.agent-bubble.minimizer { border-left: 4px solid #1f77b4; }
.agent-bubble.defender { border-left: 4px solid #2ca02c; }
.agent-bubble.adversary { border-left: 4px solid #d62728; }
.agent-bubble.judge { border-left: 4px solid #9467bd; }
.agent-bubble.system { border-left: 4px solid #7f7f7f; }
.agent-meta { color: #5f6368; font-size: 0.82rem; margin-bottom: 0.28rem; }
.agent-message { font-size: 0.94rem; line-height: 1.48; white-space: pre-wrap; }
.agent-reward { margin-top: 0.35rem; font-size: 0.82rem; color: #444; }
</style>
""",
        unsafe_allow_html=True,
    )
    chunks = ['<div class="agent-chat-stream">']
    for item in messages:
        reward = "" if item.reward is None else f'<div class="agent-reward">奖励分：{item.reward:.2f}</div>'
        chunks.append(
            (
                f'<div class="agent-chat-row {escape(item.side)}">'
                f'<div class="agent-bubble {escape(item.tone)}">'
                f'<div class="agent-meta">第 {item.attempt or "-"} 轮 · '
                f'{escape(item.agent_label)} · {escape(item.phase)}</div>'
                f'<div class="agent-message">{escape(item.message)}</div>'
                f"{reward}</div></div>"
            )
        )
    chunks.append("</div>")
    st.markdown("\n".join(chunks), unsafe_allow_html=True)


def _render_overview(st, runs: list[DashboardRun]) -> None:
    table_rows = [_table_row(item) for item in runs]
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    chart_rows = [
        {
            "运行": item.record.run_id[-8:],
            "LOC 变化": item.loc_delta or 0,
            "CC 变化": item.cc_delta or 0,
            "奖励分": item.reward or 0,
        }
        for item in reversed(runs)
    ]
    left, right = st.columns(2)
    left.subheader("代码规模与复杂度变化")
    left.bar_chart(chart_rows, x="运行", y=["LOC 变化", "CC 变化"])
    right.subheader("奖励分趋势")
    right.line_chart(chart_rows, x="运行", y="奖励分")


def _render_run_detail(st, active: DashboardRun) -> None:
    st.subheader(f"{active.record.repo_name} / {_status_label(active.record.status)}")
    columns = st.columns(5)
    columns[0].metric("LOC", f"{active.record.pre_loc} -> {active.record.post_loc}")
    columns[1].metric("LOC 压缩率", _format_percent(active.loc_reduction_percent))
    columns[2].metric("圈复杂度", f"{active.record.pre_cc} -> {active.record.post_cc}")
    columns[3].metric("CC 压缩率", _format_percent(active.cc_reduction_percent))
    columns[4].metric("奖励分", _format_float(active.reward))

    st.code(_record_summary(active), language="text")
    if active.record.error_message:
        st.error(active.record.error_message)

    st.markdown(f"工作区：`{active.workspace_path}`")
    if not active.candidate_files:
        st.info("这个运行没有可预览的 Python 候选文件。")
        return

    selected_file = st.selectbox(
        "候选代码文件",
        [str(path.relative_to(active.workspace_path)) for path in active.candidate_files],
    )
    file_path = active.workspace_path / selected_file
    st.code(file_path.read_text(encoding="utf-8"), language="python")


def _render_trajectory(st, active: DashboardRun) -> None:
    st.subheader(f"运行轨迹：{active.record.run_id}")
    if not active.trajectory:
        st.info("这个运行没有 trajectory.jsonl。")
        return

    rows = []
    for step in active.trajectory:
        reward = step.get("reward") if isinstance(step.get("reward"), dict) else {}
        rows.append(
            {
                "轮次": step.get("attempt"),
                "Agent": _agent_label(step.get("agent")),
                "阶段": _phase_label(step.get("status")),
                "说明": step.get("message", "")[:240],
                "奖励分": reward.get("reward"),
                "LOC 收益": reward.get("delta_loc"),
                "CC 收益": reward.get("delta_cc"),
                "变异杀伤率": reward.get("mutation_kill_rate"),
                "对抗测试": reward.get("adversarial_passed"),
                "附加信息": _metadata_summary(step.get("metadata")),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("原始 JSONL 轨迹"):
        st.json(active.trajectory, expanded=False)


def _select_run(st, runs: list[DashboardRun], key: str = "detail_run") -> DashboardRun:
    options = {
        f"{item.record.repo_name} | {_status_label(item.record.status)} | {item.record.run_id}": item
        for item in runs
    }
    selected = st.selectbox("选择运行记录", list(options), key=key)
    return options[selected]


if __name__ == "__main__":
    dashboard_main()
