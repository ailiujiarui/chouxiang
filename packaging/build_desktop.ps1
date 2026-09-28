# Build the Nailong desktop pet into dist/Nailong/Nailong.exe using PyInstaller.
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$pyinstaller = Join-Path $repo ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path -LiteralPath $pyinstaller)) {
    throw "PyInstaller is missing. Run: .venv\Scripts\python.exe -m pip install pyinstaller"
}

& $pyinstaller (Join-Path $PSScriptRoot "nailong.spec") --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$output = Join-Path $repo "dist\Nailong\Nailong.exe"
Write-Host "Built: $output"
Write-Host "Copy the whole dist\Nailong folder; put a .env next to Nailong.exe for DEEPSEEK_API_KEY / NAILONG_* settings."
