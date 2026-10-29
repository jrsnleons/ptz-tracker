#!/bin/zsh
# Sets up the Python environment on first run, then starts PTZ Tracker.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v uv >/dev/null || { echo "Install uv first: brew install uv"; exit 1; }
  uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt || exit 1
fi
exec .venv/bin/python -m ptztracker
