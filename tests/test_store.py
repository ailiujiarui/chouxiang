from pathlib import Path

from refactor_agent.models import RunRecord
from refactor_agent.store import SQLiteRunStore


def test_store_round_trip(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "runs.sqlite")
    record = RunRecord(
        run_id="run-1",
        repo_name="repo",
        pre_loc=10,
        post_loc=2,
        pre_cc=4,
        post_cc=1,
        self_heal_count=1,
        status="SUCCESS",
    )
    store.save(record)
    loaded = store.get("run-1")
    assert loaded == record
