from pathlib import Path

from refactor_agent.memory import build_memory_context, error_signature, target_memory_key
from refactor_agent.models import TrajectoryMemoryRecord


def test_build_memory_context_renders_chinese_state_action_reward_memory():
    context = build_memory_context(
        [
            TrajectoryMemoryRecord(
                memory_id="m1",
                run_id="run-1",
                repo_name="repo",
                target_path="leap_year.py",
                status="FAILED",
                lesson="不要再把 1900 当闰年。",
                error_signature="AssertionError: assert True is False",
            )
        ]
    )
    assert context is not None
    assert "历史轨迹记忆" in context
    assert "不要再把 1900 当闰年" in context
    assert "错误签名" in context


def test_error_signature_extracts_assertion_error():
    signature = error_signature("noise\nE   AssertionError: assert True is False\nmore noise")
    assert signature == "AssertionError: assert True is False"


def test_target_memory_key_is_repo_portable():
    assert target_memory_key(Path(__file__)) == "test_memory.py"
