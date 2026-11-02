#!/bin/zsh
# Builds dist/PTZ Tracker.app with PyInstaller (ad-hoc signed, runs on this Mac) and dist/PTZ-Tracker-mac.zip.
# PYTHON overrides the interpreter (CI uses the one from setup-python); the version comes from ptztracker/version.py.
set -e
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/ptz_tracker.spec
codesign --force --deep --sign - "dist/PTZ Tracker.app"
ditto -c -k --keepParent "dist/PTZ Tracker.app" dist/PTZ-Tracker-mac.zip
echo "Built dist/PTZ Tracker.app ($(du -sh "dist/PTZ Tracker.app" | cut -f1)) and dist/PTZ-Tracker-mac.zip"
