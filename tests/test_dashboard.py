from pathlib import Path

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
