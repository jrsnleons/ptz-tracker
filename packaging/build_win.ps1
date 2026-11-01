# Builds dist\PTZ Tracker\PTZ Tracker.exe, a zip, and (if Inno Setup is installed) dist\PTZ-Tracker-Setup.exe.
# Run on Windows 10/11 x64 with Python 3.12. Set $env:PTZ_VERSION to stamp a version (default 1.1.2).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not $env:PTZ_VERSION) { $env:PTZ_VERSION = "1.1.2" }
if (-not (Test-Path .venv-win)) { py -3.12 -m venv .venv-win }
.\.venv-win\Scripts\python -m pip install -r requirements-windows.txt
.\.venv-win\Scripts\python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\ptz_tracker_win.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Compress-Archive -Force -Path "dist\PTZ Tracker" -DestinationPath "dist\PTZ-Tracker-windows.zip"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
if ($iscc) {
    & $iscc "/DAppVersion=$env:PTZ_VERSION" packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Built dist\PTZ-Tracker-Setup.exe"
} else {
    Write-Host "Inno Setup not found; skipped the installer (winget install JRSoftware.InnoSetup)"
}
Write-Host "Built dist\PTZ Tracker\PTZ Tracker.exe and dist\PTZ-Tracker-windows.zip"
