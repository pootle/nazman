#!/bin/bash
# dev-env.sh - Create a local Python virtual environment for development and
# testing (in the repo's ./venv). This script does NOT touch /opt/nazman or any
# live system configuration; it is safe to run without sudo.
#
# Prefers python3.13 (current Python libraries break on 3.14+). On systems
# without it (e.g. Raspberry Pi OS Bookworm, which ships 3.11), falls back to
# any python3 >= 3.9.
#
# Usage: ./dev-env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "==============================================="
echo "NAZMan - dev-env.sh (local development venv)"
echo "==============================================="

# Resolve a usable interpreter: prefer python3.13 explicitly, else any modern
# python3. The --version probe also guards against non-functional stubs.
pick_python() {
    if command -v python3.13 &>/dev/null && python3.13 --version &>/dev/null; then
        echo "python3.13"
        return 0
    fi
    if command -v python3 &>/dev/null && python3 --version &>/dev/null && \
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        echo "python3"
        return 0
    fi
    return 1
}

PY_BIN="$(pick_python || true)"
if [[ -z "$PY_BIN" ]]; then
    echo "ERROR: no Python 3.9+ interpreter found (python3.13 or python3)." >&2
    echo "  - On Raspberry Pi OS Trixie / Ubuntu: install python3-venv (or run 'sudo ./prepare.sh')." >&2
    echo "  - On Raspberry Pi OS Bookworm, python3 provides 3.11 and is used automatically." >&2
    exit 1
fi
echo "Using $PY_BIN ($("$PY_BIN" --version 2>&1))."

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