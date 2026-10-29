#!/bin/zsh
# Builds dist/PTZ Tracker.app with PyInstaller (ad-hoc signed, runs on this Mac).
set -e
cd "$(dirname "$0")/.."
.venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/ptz_tracker.spec
codesign --force --deep --sign - "dist/PTZ Tracker.app"
echo "Built dist/PTZ Tracker.app ($(du -sh "dist/PTZ Tracker.app" | cut -f1))"
