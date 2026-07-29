from pathlib import Path


def test_ci_workflow_has_unit_matrix_and_docker_demo_without_secrets():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "3.11" in workflow
    assert "3.12" in workflow
    assert "pytest -q" in workflow
    assert "git diff --check" in workflow
    assert "docker/sandbox.Dockerfile" in workflow
    assert "--sandbox-backend docker" in workflow
    assert "refactor-agent benchmark" in workflow
    assert "docker/Dockerfile.benchmark" in workflow
    assert "--manifest benchmarks/manifest.toml" in workflow
    assert "--provider mock" in workflow
    assert "streamlit" in workflow
    assert "8501" in workflow
    assert "DEEPSEEK_API_KEY" not in workflow
    assert "GITHUB_TOKEN" not in workflow


def test_runtime_defaults_to_deepseek_and_reports_missing_key_without_mock_fallback():
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    startup = Path("scripts/start.ps1").read_text(encoding="utf-8")

    assert "REFACTOR_AGENT_MOCK_LLM:-false" in compose
    assert '$env:REFACTOR_AGENT_MOCK_LLM = "false"' in startup
    assert "DEEPSEEK_API_KEY is not configured. LLM task submission will be disabled." in startup
    assert "REFACTOR_AGENT_MOCK_LLM=true" in startup
