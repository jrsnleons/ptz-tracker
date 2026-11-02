# Builds dist\PTZ Tracker\PTZ Tracker.exe, a zip, and (if Inno Setup is installed) dist\PTZ-Tracker-Setup.exe.
# Run on Windows 10/11 x64 with Python 3.12. The version comes from ptztracker\version.py.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path .venv-win)) { py -3.12 -m venv .venv-win }
.\.venv-win\Scripts\python -m pip install -r requirements-windows.txt
$env:PTZ_VERSION = (.\.venv-win\Scripts\python -c "from ptztracker.version import __version__; print(__version__)").Trim()
Write-Host "Building PTZ Tracker $env:PTZ_VERSION"
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
