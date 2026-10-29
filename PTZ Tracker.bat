@echo off
rem First run installs Python deps into .venv-win (needs Python 3.12 from python.org), then starts the app.
cd /d "%~dp0"
if not exist .venv-win\Scripts\python.exe (
  py -3.12 -m venv .venv-win || (echo Install Python 3.12 from python.org first & pause & exit /b 1)
  .venv-win\Scripts\python -m pip install -r requirements-windows.txt || (pause & exit /b 1)
)
start "" .venv-win\Scripts\pythonw.exe -m ptztracker
