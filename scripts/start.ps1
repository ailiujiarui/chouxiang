[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Down,
    [switch]$Follow,
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$DashboardPort = 8501,
    [string]$PythonBaseImage = "python:3.12-slim"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop first."
}

$savedErrorAction = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& docker info *> $null
$dockerInfoExitCode = $LASTEXITCODE
$ErrorActionPreference = $savedErrorAction
if ($dockerInfoExitCode -ne 0) {
    throw "Docker Desktop is not running or is not reachable."
}

$compose = @("compose", "--project-name", "refactor-agent-local")
$env:REFACTOR_AGENT_API_PORT = $ApiPort
$env:REFACTOR_AGENT_DASHBOARD_PORT = $DashboardPort
$env:PYTHON_BASE_IMAGE = $PythonBaseImage
if (-not $env:REFACTOR_AGENT_MOCK_LLM) {
    $env:REFACTOR_AGENT_MOCK_LLM = if ($env:DEEPSEEK_API_KEY) { "false" } else { "true" }
}

if ($Down) {
    & docker @compose down
    exit $LASTEXITCODE
}

foreach ($port in @($ApiPort, $DashboardPort)) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $port is already in use. Choose another port with -ApiPort or -DashboardPort."
    }
}

$sandboxImage = "refactor-agent-sandbox:py312"
$ErrorActionPreference = "SilentlyContinue"
& docker image inspect $sandboxImage *> $null
$sandboxMissing = $LASTEXITCODE -ne 0
$ErrorActionPreference = $savedErrorAction
if ($Build -or $sandboxMissing) {
    & docker build --build-arg "PYTHON_BASE_IMAGE=$PythonBaseImage" -f docker/sandbox.Dockerfile -t $sandboxImage .
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build the sandbox image. Check registry access or retry with -PythonBaseImage <registry>/python:3.12-slim."
    }
}

$upArgs = @("up", "-d")
if ($Build) {
    $upArgs += "--build"
}
& docker @compose @upArgs
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed to start the local services."
}

try {
    $deadline = (Get-Date).AddMinutes(2)
    do {
        Start-Sleep -Seconds 2
        $api = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$ApiPort/health" } catch { $null }
        $dashboard = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$DashboardPort/_stcore/health" } catch { $null }
        if ($api.StatusCode -eq 200 -and $dashboard.StatusCode -eq 200) { break }
    } while ((Get-Date) -lt $deadline)
} catch {
    & docker @compose logs --tail 80 api dashboard
    throw
}

if ($api.StatusCode -ne 200 -or $dashboard.StatusCode -ne 200) {
    & docker @compose ps
    & docker @compose logs --tail 80 api dashboard
    throw "Services did not become healthy within two minutes."
}

Write-Host "Refactor Agent API:       http://127.0.0.1:$ApiPort"
Write-Host "Refactor Agent Dashboard: http://127.0.0.1:$DashboardPort"
Write-Host "Local Admin Token:         local-admin-secret"
$productMode = if ($env:REFACTOR_AGENT_MOCK_LLM -eq "true") { "demo" } else { "deepseek" }
Write-Host "Product Mode:              $productMode"
if ($productMode -eq "demo") {
    Write-Host "Demo limitation: only built-in deterministic patterns are supported."
}
Write-Host "All analysis is local-only; no remote repository writes."

if ($Follow) {
    & docker @compose logs -f api dashboard
    exit $LASTEXITCODE
}
