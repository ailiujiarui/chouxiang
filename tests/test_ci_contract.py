from pathlib import Path


def test_ci_workflow_has_unit_matrix_and_docker_demo_without_secrets():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "3.11" in workflow
    assert "3.12" in workflow
    assert "pytest -q" in workflow
    assert "git diff --check" in workflow
    assert "docker/sandbox.Dockerfile" in workflow
    assert "--sandbox-backend docker" in workflow
    assert "DEEPSEEK_API_KEY" not in workflow
    assert "GITHUB_TOKEN" not in workflow
