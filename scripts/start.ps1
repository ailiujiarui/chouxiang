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
        "PIP_INDEX_URL"
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

function Assert-PortAvailable([int]$port) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $port is already used by another process. Stop that process, then run start.cmd again."
    }
}

function Test-Healthy([string]$url) {
    $response = try {
        Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 3
    } catch {
        $null
    }
    return $response -and $response.StatusCode -eq 200
}

function Show-LogTail([string]$path) {
    if (Test-Path -LiteralPath $path) {
        Write-Host "--- $path ---"
        Get-Content -LiteralPath $path -Tail 80 -ErrorAction SilentlyContinue
    }
}

Import-LocalEnvironment

if (-not $env:REFACTOR_AGENT_MOCK_LLM) {
    $env:REFACTOR_AGENT_MOCK_LLM = "false"
}
if (-not $env:REFACTOR_AGENT_ALLOWED_REPOSITORIES) {
    $env:REFACTOR_AGENT_ALLOWED_REPOSITORIES = "owner/repository"
}

$systemPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $systemPython) {
    throw "Python was not found. Install Python 3.11 or newer, then run start.cmd again."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git was not found. Install Git for Windows, then run start.cmd again."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop, start it, then run start.cmd again."
}

$savedErrorAction = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& docker info *> $null
$dockerInfoExitCode = $LASTEXITCODE
$ErrorActionPreference = $savedErrorAction
if ($dockerInfoExitCode -ne 0) {
    throw "Docker Desktop is not running or is not reachable. Start Docker Desktop, then run start.cmd again."
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

$dataDirectory = Join-Path $repoRoot ".runs"
$logDirectory = Join-Path $dataDirectory "logs"
$githubWorkspaceDirectory = Join-Path $dataDirectory "github-workspaces"
New-Item -ItemType Directory -Force -Path $dataDirectory, $logDirectory, $githubWorkspaceDirectory | Out-Null

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:REFACTOR_AGENT_RUN_ROOT = $dataDirectory
$env:REFACTOR_AGENT_DATABASE = Join-Path $dataDirectory "refactor_agent.sqlite"
$env:REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT = $githubWorkspaceDirectory
$env:REFACTOR_AGENT_SANDBOX_BACKEND = "docker"
$env:REFACTOR_AGENT_DASHBOARD_DB = $env:REFACTOR_AGENT_DATABASE
$env:REFACTOR_AGENT_API_URL = "http://127.0.0.1:8000"
Remove-Item Env:REFACTOR_AGENT_SANDBOX_VOLUME -ErrorAction SilentlyContinue
Remove-Item Env:REFACTOR_AGENT_SANDBOX_DATA_ROOT -ErrorAction SilentlyContinue

$apiPattern = "-m\s+refactor_agent\.cli\s+serve(?:\s|$)"
$dashboardPattern = "-m\s+streamlit\s+run.*refactor_agent[\\/]dashboard\.py.*--server\.port\s+8501"
$nailongPattern = "-m\s+nailong_agent(?:\s|$).*--data-dir\s+`"?" + [regex]::Escape($dataDirectory)

$apiProcess = Find-ManagedProcess $apiPattern $venvPythonw
if (-not $apiProcess) {
    Assert-PortAvailable 8000
    $apiOut = Join-Path $logDirectory "api.stdout.log"
    $apiError = Join-Path $logDirectory "api.stderr.log"
    Remove-Item -LiteralPath $apiOut, $apiError -Force -ErrorAction SilentlyContinue
    $apiProcess = Start-Process `
        -FilePath $venvPythonw `
        -ArgumentList @("-m", "refactor_agent.cli", "serve", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut `
        -RedirectStandardError $apiError `
        -PassThru
    Start-Sleep -Seconds 1
    if ($apiProcess.HasExited) {
        Show-LogTail $apiError
        throw "Local API exited during startup with code $($apiProcess.ExitCode)."
    }
    $apiProcessId = Get-ManagedProcessId $apiProcess
    Write-Host "Local API:                started (PID $apiProcessId)"
} else {
    $apiProcessId = Get-ManagedProcessId $apiProcess
    Write-Host "Local API:                already running (PID $apiProcessId)"
}
Set-Content -LiteralPath (Join-Path $dataDirectory "api.pid") -Value $apiProcessId -Encoding ascii

$dashboardProcess = Find-ManagedProcess $dashboardPattern $venvPythonw
if (-not $dashboardProcess) {
    Assert-PortAvailable 8501
    $dashboardOut = Join-Path $logDirectory "dashboard.stdout.log"
    $dashboardError = Join-Path $logDirectory "dashboard.stderr.log"
    Remove-Item -LiteralPath $dashboardOut, $dashboardError -Force -ErrorAction SilentlyContinue
    $dashboardScript = Join-Path $repoRoot "src\refactor_agent\dashboard.py"
    $dashboardProcess = Start-Process `
        -FilePath $venvPythonw `
        -ArgumentList @(
            "-m", "streamlit", "run", "`"$dashboardScript`"",
            "--server.address", "127.0.0.1",
            "--server.port", "8501",
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false"
        ) `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $dashboardOut `
        -RedirectStandardError $dashboardError `
        -PassThru
    Start-Sleep -Seconds 1
    if ($dashboardProcess.HasExited) {
        Show-LogTail $dashboardError
        throw "Dashboard exited during startup with code $($dashboardProcess.ExitCode)."
    }
    $dashboardProcessId = Get-ManagedProcessId $dashboardProcess
    Write-Host "Dashboard:                started (PID $dashboardProcessId)"
} else {
    $dashboardProcessId = Get-ManagedProcessId $dashboardProcess
    Write-Host "Dashboard:                already running (PID $dashboardProcessId)"
}
Set-Content -LiteralPath (Join-Path $dataDirectory "dashboard.pid") -Value $dashboardProcessId -Encoding ascii

$deadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 2
    $apiHealthy = Test-Healthy "http://127.0.0.1:8000/health"
    $dashboardHealthy = Test-Healthy "http://127.0.0.1:8501/_stcore/health"
    if ($apiHealthy -and $dashboardHealthy) {
        break
    }
} while ((Get-Date) -lt $deadline)

if (-not $apiHealthy -or -not $dashboardHealthy) {
    Show-LogTail (Join-Path $logDirectory "api.stderr.log")
    Show-LogTail (Join-Path $logDirectory "dashboard.stderr.log")
    throw "Local services did not become healthy within two minutes. Run stop.cmd, then review .runs\logs."
}

$nailongProcess = Find-ManagedProcess $nailongPattern $venvPythonw
if (-not $nailongProcess) {
    $nailongOut = Join-Path $logDirectory "nailong.stdout.log"
    $nailongError = Join-Path $logDirectory "nailong.stderr.log"
    Remove-Item -LiteralPath $nailongOut, $nailongError -Force -ErrorAction SilentlyContinue
    $nailongProcess = Start-Process `
        -FilePath $venvPythonw `
        -ArgumentList @(
            "-m", "nailong_agent",
            "--analysis-url", "http://127.0.0.1:8000",
            "--data-dir", "`"$dataDirectory`""
        ) `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $nailongOut `
        -RedirectStandardError $nailongError `
        -PassThru
    Start-Sleep -Seconds 1
    if ($nailongProcess.HasExited) {
        Show-LogTail $nailongError
        throw "Nailong Desktop exited during startup with code $($nailongProcess.ExitCode)."
    }
    $nailongProcessId = Get-ManagedProcessId $nailongProcess
    Write-Host "Nailong Desktop:          started (PID $nailongProcessId)"
} else {
    $nailongProcessId = Get-ManagedProcessId $nailongProcess
    Write-Host "Nailong Desktop:          already running (PID $nailongProcessId)"
}
Set-Content -LiteralPath (Join-Path $dataDirectory "nailong-desktop.pid") -Value $nailongProcessId -Encoding ascii

Write-Host "Refactor Agent API:       http://127.0.0.1:8000"
Write-Host "Refactor Agent Dashboard: http://127.0.0.1:8501"
if ($env:REFACTOR_AGENT_MOCK_LLM -eq "true") {
    Write-Host "Product Mode:             offline demo"
} elseif ($env:DEEPSEEK_API_KEY) {
    Write-Host "Product Mode:             DeepSeek"
} else {
    Write-Warning "DEEPSEEK_API_KEY is not configured. The product is running, but LLM task entry points are disabled."
}

Start-Process "http://127.0.0.1:8501"
