$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

function Import-LocalEnvironment {
    $environmentFile = Join-Path $repoRoot ".env"
    if (-not (Test-Path -LiteralPath $environmentFile)) {
        return
    }

    $allowedNames = @(
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "NAILONG_DEEPSEEK_MODEL",
        "REFACTOR_AGENT_MOCK_LLM",
        "REFACTOR_AGENT_ADMIN_TOKEN",
        "REFACTOR_AGENT_ALLOWED_REPOSITORIES",
        "REFACTOR_AGENT_SQLITE_JOURNAL_MODE",
        "REFACTOR_AGENT_SQLITE_BUSY_TIMEOUT_MS",
        "PYTHON_BASE_IMAGE",
        "PIP_INDEX_URL",
        "REFACTOR_AGENT_STARTUP_TIMEOUT_SECONDS",
        "REFACTOR_AGENT_STARTUP_PORT_FALLBACK",
        "REFACTOR_AGENT_MAX_RESTARTS",
        "REFACTOR_AGENT_WATCHDOG"
    )

    foreach ($line in Get-Content -LiteralPath $environmentFile -Encoding utf8) {
        $value = $line.Trim()
        if (-not $value -or $value.StartsWith("#") -or -not $value.Contains("=")) {
            continue
        }
        $name, $rawValue = $value.Split("=", 2)
        $name = $name.Trim()
        if ($name -notin $allowedNames -or (Test-Path "Env:$name")) {
            continue
        }
        $rawValue = $rawValue.Trim()
        if (
            $rawValue.Length -ge 2 -and
            (($rawValue.StartsWith('"') -and $rawValue.EndsWith('"')) -or
             ($rawValue.StartsWith("'") -and $rawValue.EndsWith("'")))
        ) {
            $rawValue = $rawValue.Substring(1, $rawValue.Length - 2)
        }
        Set-Item -Path "Env:$name" -Value $rawValue
    }
}

function Find-ManagedProcess([string]$commandPattern, [string]$pythonwPath) {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match $commandPattern -and
        (Test-ManagedInterpreter $_ $pythonwPath)
    } | Select-Object -First 1
}

function Test-ManagedInterpreter([object]$process, [string]$pythonwPath) {
    $escapedPythonwPath = [regex]::Escape($pythonwPath)
    return (
        [string]::Equals($process.ExecutablePath, $pythonwPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $process.CommandLine -match "^\s*`"?$escapedPythonwPath`"?\s"
    )
}

function Get-ManagedProcessId([object]$process) {
    if ($null -ne $process.ProcessId) {
        return [int]$process.ProcessId
    }
    return [int]$process.Id
}

function Find-PortOwner([int]$port) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) {
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $process) {
        return $null
    }
    return [pscustomobject]@{ ProcessId = $listener.OwningProcess; Name = $process.Name; CommandLine = $process.CommandLine }
}

function Resolve-Port([int]$preferred, [string]$label) {
    if (-not (Find-PortOwner $preferred)) {
        return $preferred
    }
    if ($portFallbackEnabled) {
        for ($candidate = $preferred + 1; $candidate -lt $preferred + 20; $candidate++) {
            if (-not (Find-PortOwner $candidate)) {
                Write-Warning "Port $preferred for $label is in use; using port $candidate instead."
                return $candidate
            }
        }
        throw "Could not find a free port near $preferred for $label."
    }
    $owner = Find-PortOwner $preferred
    $ownerDetail = "PID $($owner.ProcessId) ($($owner.Name))"
    if ($owner.CommandLine) {
        $ownerDetail += "`n  command: $($owner.CommandLine)"
    }
    throw (
        "Port $preferred for $label is already in use by $ownerDetail.`n" +
        "  Stop that process, or run stop.cmd if it is a leftover managed process, then run start.cmd again.`n" +
        "  To auto-pick a free port instead, set REFACTOR_AGENT_STARTUP_PORT_FALLBACK=1 in .env."
    )
}

function Test-Healthy([string]$url) {
    $response = try {
        Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 3
    } catch {
        $null
    }
    return $response -and $response.StatusCode -eq 200
}

function Wait-Healthy([string]$url, [int]$processId, [string]$label) {
    $deadline = (Get-Date).AddSeconds($startupTimeoutSeconds)
    $delay = 1
    while ((Get-Date) -lt $deadline) {
        if (Test-Healthy $url) {
            return $true
        }
        if ($processId -gt 0 -and -not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            Write-Warning "$label process exited while waiting for health; giving up on it."
            return $false
        }
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min($delay * 2, 5)
    }
    return $false
}

function Show-LogTail([string]$path) {
    if (Test-Path -LiteralPath $path) {
        Write-Host "--- $path ---"
        Get-Content -LiteralPath $path -Tail 80 -ErrorAction SilentlyContinue
    }
}

function Rotate-Log([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        return
    }
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    Move-Item -LiteralPath $path -Destination "$path.$stamp" -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath (Split-Path $path) -Filter "$(Split-Path $path -Leaf).*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $maxRotatedLogs |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Clear-StalePidFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        return
    }
    $raw = Get-Content -LiteralPath $path -Encoding ascii | Select-Object -First 1
    $managedProcessId = 0
    if ([int]::TryParse($raw, [ref]$managedProcessId) -and (Get-Process -Id $managedProcessId -ErrorAction SilentlyContinue)) {
        return
    }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Start-ManagedService(
    [string]$label,
    [string[]]$arguments,
    [string]$outLog,
    [string]$errLog,
    [int]$restartLimit
) {
    Rotate-Log $outLog
    Rotate-Log $errLog
    for ($attempt = 1; $attempt -le $restartLimit; $attempt++) {
        $process = Start-Process `
            -FilePath $venvPythonw `
            -ArgumentList $arguments `
            -WorkingDirectory $repoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $outLog `
            -RedirectStandardError $errLog `
            -PassThru
        Start-Sleep -Seconds 2
        if (-not $process.HasExited) {
            return $process
        }
        Write-Warning "$label exited during startup (attempt $attempt/$restartLimit, code $($process.ExitCode)). Retrying..."
        Start-Sleep -Seconds 2
    }
    Show-LogTail $errLog
    throw "$label failed to stay alive after $restartLimit startup attempts. See $errLog."
}

Import-LocalEnvironment

if (-not $env:REFACTOR_AGENT_MOCK_LLM) {
    $env:REFACTOR_AGENT_MOCK_LLM = "false"
}
if (-not $env:REFACTOR_AGENT_ALLOWED_REPOSITORIES) {
    $env:REFACTOR_AGENT_ALLOWED_REPOSITORIES = "owner/repository"
}

$startupTimeoutSeconds = if ($env:REFACTOR_AGENT_STARTUP_TIMEOUT_SECONDS) { [double]$env:REFACTOR_AGENT_STARTUP_TIMEOUT_SECONDS } else { 120 }
$portFallbackEnabled = $env:REFACTOR_AGENT_STARTUP_PORT_FALLBACK -eq "1"
$maxRestarts = if ($env:REFACTOR_AGENT_MAX_RESTARTS) { [int]$env:REFACTOR_AGENT_MAX_RESTARTS } else { 3 }
$watchdogEnabled = $env:REFACTOR_AGENT_WATCHDOG -ne "0"
$maxRotatedLogs = 5

$systemPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $systemPython) {
    throw "Python was not found. Install Python 3.11 or newer, then run start.cmd again."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git was not found. Install Git for Windows, then run start.cmd again."
}

$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCommand) {
    Write-Warning "Docker CLI was not found. Running in review-only mode: verified-refactor and URL analysis tasks are disabled, and no untrusted generated code is executed on the host. Install Docker Desktop for full sandboxed execution."
    $dockerAvailable = $false
} else {
    $savedErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & docker info *> $null
    $dockerInfoExitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedErrorAction
    if ($dockerInfoExitCode -ne 0) {
        Write-Warning "Docker Desktop is not running or is not reachable. Running in review-only mode: verified-refactor and URL analysis tasks are disabled, and no untrusted generated code is executed on the host. Start Docker Desktop to enable full sandboxed execution."
        $dockerAvailable = $false
    } else {
        $dockerAvailable = $true
    }
}

$venvDirectory = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$venvPythonw = Join-Path $venvDirectory "Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating local product environment..."
    & $systemPython.Source -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create .venv. Repair the Python venv installation, then run start.cmd again."
    }
}

$savedErrorAction = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& $venvPython -c "import PySide6, nailong_agent, refactor_agent, streamlit, uvicorn" *> $null
$dependencyCheckExitCode = $LASTEXITCODE
$ErrorActionPreference = $savedErrorAction
if ($dependencyCheckExitCode -ne 0) {
    $pipIndexUrl = if ($env:PIP_INDEX_URL) { $env:PIP_INDEX_URL } else { "https://pypi.org/simple" }
    Write-Host "Installing local product dependencies..."
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        $env:UV_DEFAULT_INDEX = $pipIndexUrl
        & $uvCommand.Source sync --extra desktop --extra dashboard
        if ($LASTEXITCODE -ne 0) {
            throw "Product dependency installation failed. Check Python package network access, then run start.cmd again."
        }
    } else {
        & $venvPython -m pip install --disable-pip-version-check --index-url $pipIndexUrl -e ".[desktop,dashboard]"
        if ($LASTEXITCODE -ne 0) {
            throw "Product dependency installation failed. Check Python package network access, then run start.cmd again."
        }
    }
}

if (-not (Test-Path -LiteralPath $venvPythonw)) {
    throw "pythonw.exe is missing from .venv. Reinstall Windows Python, remove .venv, then run start.cmd again."
}

$sandboxBackend = "subprocess"
if ($dockerAvailable) {
    $pythonBaseImage = if ($env:PYTHON_BASE_IMAGE) { $env:PYTHON_BASE_IMAGE } else { "python:3.12-slim" }
    $pipIndexUrl = if ($env:PIP_INDEX_URL) { $env:PIP_INDEX_URL } else { "https://pypi.org/simple" }
    Write-Host "Preparing secure sandbox..."
    & docker build `
        --build-arg "PYTHON_BASE_IMAGE=$pythonBaseImage" `
        --build-arg "PIP_INDEX_URL=$pipIndexUrl" `
        -f docker/sandbox.Dockerfile `
        -t refactor-agent-sandbox:py312 `
        .
    if ($LASTEXITCODE -ne 0) {
        throw "Sandbox image build failed. Check Docker and Python package network access, then run start.cmd again."
    }
    $sandboxBackend = "docker"
} else {
    Write-Host "Sandbox:                  review-only (degraded, no Docker)"
}

$dataDirectory = Join-Path $repoRoot ".runs"
$logDirectory = Join-Path $dataDirectory "logs"
$githubWorkspaceDirectory = Join-Path $dataDirectory "github-workspaces"
New-Item -ItemType Directory -Force -Path $dataDirectory, $logDirectory, $githubWorkspaceDirectory | Out-Null

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:REFACTOR_AGENT_RUN_ROOT = $dataDirectory
$env:REFACTOR_AGENT_DATABASE = Join-Path $dataDirectory "refactor_agent.sqlite"
$env:REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT = $githubWorkspaceDirectory
$env:REFACTOR_AGENT_SANDBOX_BACKEND = $sandboxBackend
$env:REFACTOR_AGENT_DASHBOARD_DB = $env:REFACTOR_AGENT_DATABASE
Remove-Item Env:REFACTOR_AGENT_SANDBOX_VOLUME -ErrorAction SilentlyContinue
Remove-Item Env:REFACTOR_AGENT_SANDBOX_DATA_ROOT -ErrorAction SilentlyContinue

Clear-StalePidFile (Join-Path $dataDirectory "api.pid")
Clear-StalePidFile (Join-Path $dataDirectory "dashboard.pid")
Clear-StalePidFile (Join-Path $dataDirectory "nailong-desktop.pid")

$apiPattern = "-m\s+refactor_agent\.cli\s+serve(?:\s|$)"
$nailongPattern = "-m\s+nailong_agent(?:\s|$).*--data-dir\s+`"?`"?" + [regex]::Escape($dataDirectory)

$apiProcess = Find-ManagedProcess $apiPattern $venvPythonw
$apiPort = 8000
if (-not $apiProcess) {
    $apiPort = Resolve-Port 8000 "Local API"
    $apiOut = Join-Path $logDirectory "api.stdout.log"
    $apiError = Join-Path $logDirectory "api.stderr.log"
    $apiProcess = Start-ManagedService "Local API" @("-m", "refactor_agent.cli", "serve", "--host", "127.0.0.1", "--port", "$apiPort") $apiOut $apiError $maxRestarts
    $apiProcessId = Get-ManagedProcessId $apiProcess
    Write-Host "Local API:                started (PID $apiProcessId, port $apiPort)"
} else {
    $apiProcessId = Get-ManagedProcessId $apiProcess
    Write-Host "Local API:                already running (PID $apiProcessId, port $apiPort)"
}
Set-Content -LiteralPath (Join-Path $dataDirectory "api.pid") -Value $apiProcessId -Encoding ascii
$apiBaseUrl = "http://127.0.0.1:$apiPort"
$env:REFACTOR_AGENT_API_URL = $apiBaseUrl

$dashboardScript = Join-Path $repoRoot "src\refactor_agent\dashboard.py"
$dashboardProcess = Find-ManagedProcess "-m\s+streamlit\s+run.*refactor_agent[\\/]dashboard\.py.*--server\.port\s+8501" $venvPythonw
$dashboardPort = 8501
if (-not $dashboardProcess) {
    $dashboardPort = Resolve-Port 8501 "Dashboard"
    $dashboardOut = Join-Path $logDirectory "dashboard.stdout.log"
    $dashboardError = Join-Path $logDirectory "dashboard.stderr.log"
    $dashboardProcess = Start-ManagedService "Dashboard" @(
        "-m", "streamlit", "run", "`"$dashboardScript`"",
        "--server.address", "127.0.0.1",
        "--server.port", "$dashboardPort",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false"
    ) $dashboardOut $dashboardError $maxRestarts
    $dashboardProcessId = Get-ManagedProcessId $dashboardProcess
    Write-Host "Dashboard:                started (PID $dashboardProcessId, port $dashboardPort)"
} else {
    $dashboardProcessId = Get-ManagedProcessId $dashboardProcess
    Write-Host "Dashboard:                already running (PID $dashboardProcessId, port $dashboardPort)"
}
Set-Content -LiteralPath (Join-Path $dataDirectory "dashboard.pid") -Value $dashboardProcessId -Encoding ascii

$apiHealthy = Wait-Healthy "$apiBaseUrl/health" $apiProcessId "Local API"
if (-not $apiHealthy) {
    Show-LogTail (Join-Path $logDirectory "api.stderr.log")
    throw "Local API did not become healthy within $startupTimeoutSeconds seconds. See .runs\logs."
}
$dashboardHealthy = Wait-Healthy "http://127.0.0.1:$dashboardPort/_stcore/health" $dashboardProcessId "Dashboard"
if (-not $dashboardHealthy) {
    Write-Warning "Dashboard did not become healthy within $startupTimeoutSeconds seconds; continuing with the API and Nailong only. See .runs\logs\dashboard.stderr.log"
}

$nailongProcess = Find-ManagedProcess $nailongPattern $venvPythonw
if (-not $nailongProcess) {
    $nailongOut = Join-Path $logDirectory "nailong.stdout.log"
    $nailongError = Join-Path $logDirectory "nailong.stderr.log"
    try {
        $nailongProcess = Start-ManagedService "Nailong Desktop" @(
            "-m", "nailong_agent",
            "--analysis-url", $apiBaseUrl,
            "--data-dir", "`"$dataDirectory`""
        ) $nailongOut $nailongError $maxRestarts
        $nailongProcessId = Get-ManagedProcessId $nailongProcess
        Write-Host "Nailong Desktop:          started (PID $nailongProcessId)"
    } catch {
        $nailongProcessId = 0
        Write-Warning "Nailong Desktop failed to start; continuing without the pet. See .runs\logs\nailong.stderr.log"
    }
} else {
    $nailongProcessId = Get-ManagedProcessId $nailongProcess
    Write-Host "Nailong Desktop:          already running (PID $nailongProcessId)"
}
if ($nailongProcessId -gt 0) {
    Set-Content -LiteralPath (Join-Path $dataDirectory "nailong-desktop.pid") -Value $nailongProcessId -Encoding ascii
}

if ($watchdogEnabled -and $apiHealthy) {
    $servicesManifest = Join-Path $dataDirectory "services.json"
    $manifest = @(
        [pscustomobject]@{
            name = "Local API"
            pid_file = "api.pid"
            args = @("-m", "refactor_agent.cli", "serve", "--host", "127.0.0.1", "--port", "$apiPort")
        }
        [pscustomobject]@{
            name = "Dashboard"
            pid_file = "dashboard.pid"
            args = @("-m", "streamlit", "run", "`"$dashboardScript`"", "--server.address", "127.0.0.1", "--server.port", "$dashboardPort", "--server.headless", "true", "--browser.gatherUsageStats", "false")
        }
        [pscustomobject]@{
            name = "Nailong Desktop"
            pid_file = "nailong-desktop.pid"
            args = @("-m", "nailong_agent", "--analysis-url", $apiBaseUrl, "--data-dir", "`"$dataDirectory`"")
        }
    ) | ConvertTo-Json -Depth 4
    Set-Content -LiteralPath $servicesManifest -Value $manifest -Encoding utf8
    Remove-Item -LiteralPath (Join-Path $dataDirectory "watchdog.stop") -Force -ErrorAction SilentlyContinue
    $watchdogScript = Join-Path $PSScriptRoot "watchdog.ps1"
    $watchdogProcess = Start-Process `
        -FilePath (Get-Command powershell).Source `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$watchdogScript`"", "`"$dataDirectory`"") `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath (Join-Path $dataDirectory "watchdog.pid") -Value $watchdogProcess.Id -Encoding ascii
    Write-Host "Watchdog:                 started (PID $watchdogProcess.Id)"
} else {
    Write-Host "Watchdog:                 disabled"
}

Write-Host "Refactor Agent API:       $apiBaseUrl"
Write-Host "Refactor Agent Dashboard: http://127.0.0.1:$dashboardPort"
if ($env:REFACTOR_AGENT_MOCK_LLM -eq "true") {
    Write-Host "Product Mode:             offline demo"
} elseif ($env:DEEPSEEK_API_KEY) {
    Write-Host "Product Mode:             DeepSeek"
} else {
    Write-Warning "DEEPSEEK_API_KEY is not configured. The product is running, but LLM task entry points are disabled."
}

if ($dashboardHealthy) {
    Start-Process "http://127.0.0.1:$dashboardPort"
}
