# Builds dist\PTZ Tracker\PTZ Tracker.exe (and a zip). Run on Windows 10/11 x64 with Python 3.12.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path .venv-win)) { py -3.12 -m venv .venv-win }
.\.venv-win\Scripts\python -m pip install -r requirements-windows.txt
.\.venv-win\Scripts\python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\ptz_tracker_win.spec
Compress-Archive -Force -Path "dist\PTZ Tracker" -DestinationPath "dist\PTZ-Tracker-windows.zip"
Write-Host "Built dist\PTZ Tracker\PTZ Tracker.exe and dist\PTZ-Tracker-windows.zip"
