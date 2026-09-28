$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

$dataDirectory = Join-Path $repoRoot ".runs"
$venvPythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"

function Test-ManagedProcess([object]$process, [string]$commandPattern) {
    $escapedPythonwPath = [regex]::Escape($venvPythonw)
    return (
        $process -and
        $process.CommandLine -match $commandPattern -and
        (
            [string]::Equals(
                $process.ExecutablePath,
                $venvPythonw,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            $process.CommandLine -match "^\s*`"?$escapedPythonwPath`"?\s"
        )
    )
}

function Stop-ProductProcess([string]$name, [string]$pidFileName, [string]$commandPattern) {
    $pidFile = Join-Path $dataDirectory $pidFileName
    $stoppedIds = @()

    if (Test-Path -LiteralPath $pidFile) {
        $rawProcessId = Get-Content -LiteralPath $pidFile -Encoding ascii | Select-Object -First 1
        $managedProcessId = 0
        if ([int]::TryParse($rawProcessId, [ref]$managedProcessId)) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $managedProcessId" -ErrorAction SilentlyContinue
            if (Test-ManagedProcess $process $commandPattern) {
                Stop-Process -Id $managedProcessId -Force
                $stoppedIds += $managedProcessId
            }
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }

    $remaining = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        (Test-ManagedProcess $_ $commandPattern) -and $_.ProcessId -notin $stoppedIds
    }
    foreach ($process in $remaining) {
        Stop-Process -Id $process.ProcessId -Force
        $stoppedIds += $process.ProcessId
    }

    if ($stoppedIds.Count -gt 0) {
        Write-Host "$name`: stopped"
    } else {
        Write-Host "$name`: not running"
    }
}

$escapedDataDirectory = [regex]::Escape($dataDirectory)
$apiPattern = "-m\s+refactor_agent\.cli\s+serve(?:\s|$)"
$dashboardPattern = "-m\s+streamlit\s+run.*refactor_agent[\\/]dashboard\.py.*--server\.port\s+\d+"
$nailongPattern = "-m\s+nailong_agent(?:\s|$).*--data-dir\s+`"?" + $escapedDataDirectory

Set-Content -LiteralPath (Join-Path $dataDirectory "watchdog.stop") -Value "stop" -Encoding ascii -ErrorAction SilentlyContinue
Stop-ProductProcess "Watchdog" "watchdog.pid" "watchdog\.ps1"
Stop-ProductProcess "Nailong Desktop" "nailong-desktop.pid" $nailongPattern
Stop-ProductProcess "Dashboard" "dashboard.pid" $dashboardPattern
Stop-ProductProcess "Local API" "api.pid" $apiPattern
Remove-Item -LiteralPath (Join-Path $dataDirectory "watchdog.stop") -Force -ErrorAction SilentlyContinue

Write-Host "Local databases, logs, and sandbox image were preserved."
