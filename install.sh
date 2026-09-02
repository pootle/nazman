#!/bin/bash
# install.sh - One-shot bootstrap of NAZMan on a fresh Ubuntu / Raspberry Pi OS.
#
# This is the entry point for the documented one-liner:
#
#   sudo curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | bash
#
# It downloads the production branch of the repo into /opt/nazman and then runs
# the existing prepare.sh (system deps) and build.sh (service setup) scripts
# that ship with the project. Idempotent: safe to re-run on an existing install.
#
# Usage: sudo bash install.sh [<repo_url>] [<branch>]

set -euo pipefail

DEST="/opt/nazman"
DEFAULT_URL="https://github.com/pootle/nazman.git"
DEFAULT_BRANCH="main"

REPO_URL="${1:-$DEFAULT_URL}"
BRANCH="${2:-$DEFAULT_BRANCH}"

echo "==============================================="
echo "NAZMan - install.sh (one-shot bootstrap)"
echo "  repo:   $REPO_URL (branch: $BRANCH)"
echo "  target: $DEST"
echo "==============================================="

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    echo "Usage: sudo curl -fsSL <url>/install.sh | bash" >&2
    exit 1
fi

# Best-effort Debian / Raspberry Pi OS (apt) detection.
if ! command -v apt-get &>/dev/null; then
    echo "ERROR: apt-get not found. NAZMan requires a Debian-based OS" >&2
    echo "such as Ubuntu Server or Raspberry Pi OS." >&2
    exit 1
fi

# Bootstrap tooling needed to clone the repo.
if ! command -v git &>/dev/null; then
    echo "Installing git..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y git
fi

echo "Fetching NAZMan ($BRANCH) into $DEST..."
if [[ -d "$DEST/.git" ]]; then
    # Existing clone: update it.
    git -C "$DEST" fetch --all --quiet
    git -C "$DEST" checkout --quiet "$BRANCH"
    git -C "$DEST" pull --quiet origin "$BRANCH"
else
    git clone --quiet --branch "$BRANCH" --single-branch "$REPO_URL" "$DEST"
fi

echo ""
echo "Installing system dependencies (prepare.sh)..."
bash "$DEST/prepare.sh"

echo ""
echo "Building service (build.sh)..."
bash "$DEST/build.sh"

echo ""
echo "==============================================="
echo "install.sh complete. NAZMan service is running."
echo "Use 'sudo /opt/nazman/deploy.sh' after code updates."
echo "==============================================="
