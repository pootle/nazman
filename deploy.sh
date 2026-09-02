#!/bin/bash
# deploy.sh - Update the running NAZMan service in /opt/nazman after changes.
#
# Run with sudo during development or after applying an update (e.g. git pull).
# Uses deploy.txt as the manifest of tracked files and reports ONLY files that
# are new, changed, or deleted (silent when nothing differs). If any files were
# applied, the nazman service is restarted.
#
# Usage: sudo ./deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/deploy.txt"
DEST="/opt/nazman"
SERVICE_NAME="nazman"

changed_any=0
new_count=0
changed_count=0
deleted_count=0
skipped_count=0

echo "==============================================="
echo "NAZMan - deploy.sh (update live service)"
echo "  source: $SCRIPT_DIR"
echo "  target: $DEST"
echo "==============================================="

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    exit 1
fi

if [[ ! -f "$MANIFEST" ]]; then
    echo "ERROR: $MANIFEST not found." >&2
    exit 1
fi

if [[ ! -d "$DEST" ]]; then
    echo "ERROR: $DEST does not exist. Run ./build.sh first." >&2
    exit 1
fi

report_new() {
    local rel="$1"
    echo "  [new]     $rel"
    new_count=$((new_count + 1))
}

report_changed() {
    local rel="$1"
    echo "  [changed] $rel"
    changed_count=$((changed_count + 1))
}

report_deleted() {
    local rel="$1"
    echo "  [deleted] $rel"
    deleted_count=$((deleted_count + 1))
}

apply_new() {
    local rel="$1"
    mkdir -p "$(dirname "$DEST/$rel")"
    cp "$SCRIPT_DIR/$rel" "$DEST/$rel"
    changed_any=1
}

apply_changed() {
    local rel="$1"
    cp "$SCRIPT_DIR/$rel" "$DEST/$rel"
    changed_any=1
}

apply_deleted() {
    local rel="$1"
    rm -f "$DEST/$rel"
    changed_any=1
}

while IFS= read -r rel; do
    [[ -z "$rel" || "$rel" =~ ^# ]] && continue

    src="$SCRIPT_DIR/$rel"
    dst="$DEST/$rel"

    if [[ ! -e "$src" ]]; then
        # Tracked file no longer present in the repo -> remove from target.
        if [[ -e "$dst" ]]; then
            report_deleted "$rel"
            apply_deleted "$rel"
        fi
        continue
    fi

    if [[ ! -e "$dst" ]]; then
        report_new "$rel"
        apply_new "$rel"
        continue
    fi

    # Both exist: new/unmodified unless contents differ.
    if [[ -f "$src" && -f "$dst" ]] && ! cmp -s "$src" "$dst"; then
        report_changed "$rel"
        apply_changed "$rel"
    elif [[ -d "$src" && -d "$dst" ]] && ! diff -qr "$src" "$dst" >/dev/null 2>&1; then
        report_changed "$rel"
        rm -rf "$dst"
        cp -r "$src" "$dst"
        changed_any=1
    else
        skipped_count=$((skipped_count + 1))
    fi
done < "$MANIFEST"

echo "-----------------------------------------------"
echo "Summary: $new_count new, $changed_count changed, $deleted_count deleted, $skipped_count unchanged."

if [[ "$changed_any" -eq 1 ]]; then
    echo "Clearing __pycache__ under $DEST..."
    find "$DEST" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    echo "Restarting ${SERVICE_NAME} service..."
    systemctl restart "nazman.service"
    echo "Done."
else
    echo "No changes applied; service left running."
fi