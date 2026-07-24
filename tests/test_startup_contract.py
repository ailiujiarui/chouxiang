from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_click_script_checks_docker_and_health_and_has_safe_stop():
    script = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    assert "Get-Command docker" in script
    assert "docker info" in script
    assert "/health" in script
    assert "_stcore/health" in script
    assert "$Down" in script
    assert "docker @compose down" in script
    assert "docker volume" not in script.lower()
    assert "docker image inspect" in script
    assert "docker/sandbox.Dockerfile" in script
    assert "PYTHON_BASE_IMAGE=$PythonBaseImage" in script
    assert "PIP_INDEX_URL=$PipIndexUrl" in script
    assert "PipIndexUrl" in script
    assert 'if ($env:DEEPSEEK_API_KEY) { "false" } else { "true" }' in script
    assert "Product Mode:" in script
    assert "single-user; no Admin Token" in script
    assert "Bearer token enabled" in script
    assert "local-admin-secret" not in script
    assert "no remote repository writes" in script
    assert "-PythonBaseImage <registry>/python:3.12-slim" in script
    assert "[switch]$Desktop" in script
    assert "NailongDataDir" in script
    assert '"--analysis-url", "http://127.0.0.1:$ApiPort"' in script
    assert '"--data-dir", "`"$resolvedNailongDataDir`""' in script
    assert '"--notification-database"' not in script
    assert "pythonw.exe" in script
    assert "import PySide6, nailong_agent" in script
    assert "Start-Process -FilePath $pythonwExe" in script


def test_compose_starts_api_before_dashboard_with_localhost_ports():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "  api:" in compose
    assert 'command: ["serve", "--host", "0.0.0.0", "--port", "8000"]' in compose
    assert "condition: service_healthy" in compose
    assert "http://api:8000" in compose
    assert '"--api-url", "http://api:8000"' in compose
    assert '127.0.0.1:${REFACTOR_AGENT_API_PORT:-8000}:8000' in compose
    assert '127.0.0.1:${REFACTOR_AGENT_DASHBOARD_PORT:-8501}:8501' in compose
    assert "REFACTOR_AGENT_MOCK_LLM: ${REFACTOR_AGENT_MOCK_LLM:-true}" in compose
    assert "DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:-}" in compose
    assert "PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}" in compose
    assert "REFACTOR_AGENT_SANDBOX_VOLUME: refactor-agent-local_refactor-agent-memory" in compose
    assert "REFACTOR_AGENT_DRY_RUN" not in compose
    assert "GITHUB_WEBHOOK_SECRET" not in compose
    assert "REFACTOR_AGENT_ALLOWED_SENDERS" not in compose
    assert "REFACTOR_AGENT_ADMIN_TOKEN: ${REFACTOR_AGENT_ADMIN_TOKEN:-}" in compose


def test_app_image_contains_docker_cli_for_nested_sandbox_runs():
    dockerfile = (ROOT / "docker" / "app.Dockerfile").read_text(encoding="utf-8")
    assert "docker-cli git" in dockerfile
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
