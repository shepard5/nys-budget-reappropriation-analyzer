#!/usr/bin/env bash
# Double-click to launch the NYS Reappropriation Automator (macOS).
# First run: creates a local venv, installs dependencies (~60s).
# Subsequent runs: starts immediately.
set -e
cd "$(dirname "$0")"

# Require Python 3.9+
if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "Python 3 is not installed.\n\nPlease install Python 3.11+ from python.org, then double-click run.command again." buttons {"OK"} default button 1 with icon stop'
  exit 1
fi

# Check version >=3.9
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,9) else 0)')
if [ "$PY_OK" != "1" ]; then
  osascript -e 'display dialog "Python is too old. Please install Python 3.11+ from python.org." buttons {"OK"} default button 1 with icon stop'
  exit 1
fi

# Create venv on first run
if [ ! -d ".venv_app" ]; then
  echo "First-time setup — installing dependencies (this takes about 60 seconds)..."
  python3 -m venv .venv_app
  .venv_app/bin/pip install --upgrade pip --quiet
  .venv_app/bin/pip install -r requirements.txt --quiet
  echo "Setup complete."
fi

# Launch Streamlit, open browser after a short delay
(sleep 2 && open "http://localhost:8501") &
exec .venv_app/bin/streamlit run app.py \
  --browser.gatherUsageStats false \
  --server.headless true
