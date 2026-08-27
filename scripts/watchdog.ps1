param(
    [Parameter(Mandatory = $true)][string]$dataDirectory
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$manifestPath = Join-Path $dataDirectory "services.json"
$stopFile = Join-Path $dataDirectory "watchdog.stop"
$outLog = Join-Path $dataDirectory "logs\watchdog.stdout.log"
$errLog = Join-Path $dataDirectory "logs\watchdog.stderr.log"

if (-not (Test-Path -LiteralPath $manifestPath)) {
    exit 0
}

while ($true) {
    if ((Test-Path -LiteralPath $stopFile) -or -not (Test-Path -LiteralPath $manifestPath)) {
        break
    }
    $services = try {
        Get-Content -LiteralPath $manifestPath -Encoding utf8 | ConvertFrom-Json
    } catch {
        Start-Sleep -Seconds 10
        continue
    }
    foreach ($service in $services) {
        $pidFile = Join-Path $dataDirectory $service.pid_file
        if (-not (Test-Path -LiteralPath $pidFile)) {
            continue
        }
        $raw = Get-Content -LiteralPath $pidFile -Encoding ascii | Select-Object -First 1
        $processId = 0
        if (-not [int]::TryParse($raw, [ref]$processId)) {
            continue
        }
        if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
            continue
        }
        $restarted = Start-Process `
            -FilePath $venvPythonw `
            -ArgumentList $service.args `
            -WorkingDirectory $repoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $outLog `
            -RedirectStandardError $errLog `
            -PassThru
        Set-Content -LiteralPath $pidFile -Value $restarted.Id -Encoding ascii
        Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') restarted $($service.name) as PID $($restarted.Id)"
    }
    Start-Sleep -Seconds 10
}

Remove-Item -LiteralPath (Join-Path $dataDirectory "watchdog.pid") -Force -ErrorAction SilentlyContinue
