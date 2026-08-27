from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_click_launchers_expose_only_start_and_stop() -> None:
    start_cmd = (ROOT / "start.cmd").read_text(encoding="utf-8")
    stop_cmd = (ROOT / "stop.cmd").read_text(encoding="utf-8")
    startup = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    shutdown = (ROOT / "scripts" / "stop.ps1").read_text(encoding="utf-8")

    assert "scripts\\start.ps1" in start_cmd
    assert "scripts\\stop.ps1" in stop_cmd
    assert "param(" not in startup
    assert "param(" not in shutdown
    for removed_option in ("$Build", "$Down", "$Follow", "$Desktop", "$ApiPort", "$DashboardPort"):
        assert removed_option not in startup


def test_startup_bootstraps_the_full_product_without_global_python_changes() -> None:
    script = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert "Get-Command docker" in script
    assert "docker info" in script
    assert script.index("Get-Command python") < script.index("docker/sandbox.Dockerfile")
    assert "docker/sandbox.Dockerfile" in script
    assert "docker compose" not in script.lower()
    assert "app.Dockerfile" not in script
    assert 'Join-Path $repoRoot ".venv"' in script
    assert "-m venv" in script
    assert '-e ".[desktop,dashboard]"' in script
    assert "Start-Process" in script
    assert '"-m", "refactor_agent.cli", "serve"' in script
    assert '"-m", "streamlit", "run"' in script
    assert '"-m", "nailong_agent"' in script
    assert "-WindowStyle Hidden" in script
    assert "api.pid" in script
    assert "dashboard.pid" in script
    assert "nailong-desktop.pid" in script
    assert "REFACTOR_AGENT_SANDBOX_BACKEND = $sandboxBackend" in script
    assert "$sandboxBackend = \"subprocess\"" in script
    assert "REFACTOR_AGENT_SANDBOX_VOLUME" in script
    assert ".runs\\logs" in script
    assert 'Start-Process "http://127.0.0.1:8501"' in script
    assert "/health" in script
    assert "_stcore/health" in script


def test_startup_loads_only_allowlisted_local_environment_without_printing_secrets() -> None:
    startup = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "Import-LocalEnvironment" in startup
    assert "$allowedNames" in startup
    assert "DEEPSEEK_API_KEY" in startup
    assert "Set-Item -Path \"Env:$name\"" in startup
    assert "Write-Host $rawValue" not in startup
    assert "local-admin-secret" not in startup
    assert "DEEPSEEK_API_KEY=" in example
    assert ".env" in ignore


def test_stop_targets_only_repository_product_processes_and_preserves_data() -> None:
    script = (ROOT / "scripts" / "stop.ps1").read_text(encoding="utf-8")

    assert "nailong-desktop.pid" in script
    assert "api.pid" in script
    assert "dashboard.pid" in script
    assert "refactor_agent\\.cli\\s+serve" in script
    assert "streamlit\\s+run" in script
    assert "nailong_agent" in script
    assert "ExecutablePath" in script
    assert "OrdinalIgnoreCase" in script
    assert "escapedPythonwPath" in script
    assert "Stop-Process -Id" in script
    assert "Get-CimInstance Win32_Process" in script
    assert "docker" not in script.lower()
    assert "Remove-Item -LiteralPath $dataDirectory" not in script


def test_compose_uses_fixed_localhost_ports_and_safe_runtime_defaults() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "  api:" in compose
    assert 'command: ["serve", "--host", "0.0.0.0", "--port", "8000"]' in compose
    assert "condition: service_healthy" in compose
    assert '127.0.0.1:8000:8000' in compose
    assert '127.0.0.1:8501:8501' in compose
    assert "REFACTOR_AGENT_API_PORT" not in compose
    assert "REFACTOR_AGENT_DASHBOARD_PORT" not in compose
    assert "REFACTOR_AGENT_MOCK_LLM: ${REFACTOR_AGENT_MOCK_LLM:-false}" in compose
    assert "DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:-}" in compose
    assert "DEEPSEEK_BASE_URL: ${DEEPSEEK_BASE_URL:-https://api.deepseek.com}" in compose
    assert "DEEPSEEK_MODEL: ${DEEPSEEK_MODEL:-deepseek-chat}" in compose
    assert "REFACTOR_AGENT_SANDBOX_VOLUME: refactor-agent-local_refactor-agent-memory" in compose
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
    assert "REFACTOR_AGENT_DRY_RUN" not in compose
    assert "GITHUB_WEBHOOK_SECRET" not in compose
    assert "REFACTOR_AGENT_ALLOWED_SENDERS" not in compose


def test_app_image_contains_docker_cli_for_nested_sandbox_runs() -> None:
    dockerfile = (ROOT / "docker" / "app.Dockerfile").read_text(encoding="utf-8")
    assert "docker-cli git" in dockerfile
