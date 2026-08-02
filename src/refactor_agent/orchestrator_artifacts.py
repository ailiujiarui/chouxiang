from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from refactor_agent.artifacts import RunArtifactWriter


def write_run_artifacts(
    run_root: Path,
    run_id: str,
    state: Mapping[str, Any],
    report: str,
) -> None:
    """Persist the stable artifact set produced by one orchestrator run."""
    writer = RunArtifactWriter(run_root / run_id)
    original = str(state.get("original_code") or "")
    candidate = str(state.get("current_code") or original)
    writer.write_sources(original, candidate)

    sandbox = state.get("sandbox")
    writer.write_log(
        "pytest.log",
        "\n".join(
            part
            for part in [getattr(sandbox, "stdout", ""), getattr(sandbox, "stderr", "")]
            if part
        ),
    )

    adversarial = state.get("adversarial")
    writer.write_log(
        "adversary.log",
        "\n".join(
            part
            for part in [getattr(adversarial, "stdout", ""), getattr(adversarial, "stderr", "")]
            if part
        ),
    )

    mutation = state.get("mutation")
    writer.write_json("mutation.json", mutation.model_dump(mode="json") if mutation else {})
    writer.write_report(report)
