#!/bin/bash
# dev-env.sh - Create a local Python virtual environment for development and
# testing (in the repo's ./venv). This script does NOT touch /opt/nazman or any
# live system configuration; it is safe to run without sudo.
#
# Uses python3.13 (current Python libraries break on 3.14+).
#
# Usage: ./dev-env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PY_BIN="python3.13"

echo "==============================================="
echo "NAZMan - dev-env.sh (local development venv)"
echo "==============================================="

if ! command -v "$PY_BIN" &>/dev/null; then
    echo "ERROR: $PY_BIN not found. Run 'sudo ./prepare.sh' first (installs python3.13)." >&2
    exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Creating virtual environment at $VENV_DIR using $PY_BIN..."
    "$PY_BIN" -m venv "$VENV_DIR"
else
    echo "Reusing existing virtual environment at $VENV_DIR."
fi

echo "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "Installing test tooling..."
"$VENV_DIR/bin/pip" install pytest pytest-asyncio

echo ""
echo "==============================================="
echo "dev-env.sh complete."
echo ""
echo "Run the test suite:"
echo "  $VENV_DIR/bin/python -m pytest tests/ -q"
echo ""
echo "Run the development server:"
echo "  $VENV_DIR/bin/uvicorn nazman.main:app --reload --host 0.0.0.0 --port 8080"
echo "==============================================="