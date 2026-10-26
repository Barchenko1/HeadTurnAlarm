#!/usr/bin/env bash
# Starts Head Turn Alarm (macOS and Linux).
# The first run installs what it needs into a .venv folder next to this file.
set -euo pipefail
cd "$(dirname "$0")"

if ! .venv/bin/python -c "import mediapipe, PySide6" 2>/dev/null; then
  echo "First run: installing (takes a minute or two)..."
  if ! python3 -m venv .venv; then
    echo "Could not create a Python environment."
    echo "On Ubuntu/Debian run: sudo apt install python3-venv   then start again."
    exit 1
  fi
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet mediapipe PySide6-Essentials
fi

exec .venv/bin/python head_turn_alarm.py "$@"
