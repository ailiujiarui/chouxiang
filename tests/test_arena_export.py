from pathlib import Path

from refactor_agent.arena_export import render_arena_report, write_arena_report
from refactor_agent.dashboard import load_dashboard_runs
from refactor_agent.models import RunRecord
from refactor_agent.store import SQLiteRunStore


def test_render_arena_report_contains_summary_and_rounds(tmp_path: Path):
    run_root, database = _make_arena_run(tmp_path)
    runs = load_dashboard_runs(database, run_root)

    report = render_arena_report(runs, generated_at="2026-07-09T12:00:00+08:00")

    assert "# 重构 Agent 竞技场战报" in report
    assert "成功率: 100%" in report
    assert "10 -> 2 (-8)" in report
    assert "5 -> 1 (-4)" in report
    assert "攻防回合摘录" in report
    assert "精简狂魔" in report
    assert "奖励分=12.50" in report


def test_write_arena_report_writes_markdown(tmp_path: Path):
    run_root, database = _make_arena_run(tmp_path)
    output = tmp_path / "exports" / "arena-report.md"

    path = write_arena_report(database, run_root, output, limit=5)

    assert path == output
    assert output.is_file()
    assert "重构 Agent 竞技场战报" in output.read_text(encoding="utf-8")


def test_render_arena_report_handles_empty_runs():
    report = render_arena_report([], generated_at="2026-07-09T12:00:00+08:00")

    assert "运行数: 0" in report
    assert "暂无运行记录" in report


def _make_arena_run(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / ".runs"
    database = run_root / "runs.sqlite"
    store = SQLiteRunStore(database)
    run_id = "20260709120000-demo"
    store.save(
        RunRecord(
            run_id=run_id,
            repo_name="demo-arena",
            pre_loc=10,
            post_loc=2,
            pre_cc=5,
            post_cc=1,
            self_heal_count=1,
            status="SUCCESS",
        )
    )
    trajectory_path = run_root / run_id / "trajectory.jsonl"
    trajectory_path.parent.mkdir(parents=True)
    trajectory_path.write_text(
        (
            '{"attempt":1,"status":"MINIMIZER_PROPOSED","agent":"MINIMIZER",'
            '"message":"压缩候选代码"}\n'
            '{"attempt":1,"status":"JUDGE_SCORED","agent":"JUDGE",'
            '"message":"裁决通过","reward":{"reward":12.5}}\n'
        ),
        encoding="utf-8",
    )
    return run_root, database
