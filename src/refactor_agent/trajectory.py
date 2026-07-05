from __future__ import annotations

import json
from pathlib import Path

from refactor_agent.models import MetricsSnapshot, MutationTestResult, RewardBreakdown, TrajectoryStep


def calculate_reward(
    pre: MetricsSnapshot,
    post: MetricsSnapshot,
    retry_count: int,
    mutation_result: MutationTestResult | None = None,
) -> RewardBreakdown:
    delta_loc = pre.loc - post.loc
    delta_cc = pre.cyclomatic_complexity - post.cyclomatic_complexity
    kill_rate = mutation_result.kill_rate if mutation_result else 1.0
    reward = delta_cc * 3.0 + delta_loc * 1.0 + kill_rate * 10.0 - retry_count * 2.0
    return RewardBreakdown(
        delta_loc=delta_loc,
        delta_cc=delta_cc,
        retry_count=retry_count,
        mutation_kill_rate=kill_rate,
        reward=reward,
    )


def append_trajectory(path: Path, step: TrajectoryStep) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(step.model_dump(mode="json"), ensure_ascii=False) + "\n")
