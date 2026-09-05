#!/bin/bash
# dev-live.sh - Mirror the live production NAZMan service on the dev machine.
#
# The dev test suite mocks ZFS, but live testing here runs the REAL production
# setup: apt system packages (including ZFS), the app copied to /opt/nazman, a
# venv there, /etc/nazman/nazman.conf, the nfsanon identity, and the
# nazman systemd service (root, Requires=zfs.target). This is the install.sh
# flow applied to the local checkout so you exercise exactly what ships on the
# Pi.
#
# Usage:
#   sudo ./dev-live.sh setup     # prepare.sh + build.sh (full provision; idempotent)
#   sudo ./dev-live.sh update    # deploy.sh (copy changed files + restart)
#   ./dev-live.sh status         # service status + HTTP check
#   ./dev-live.sh logs [-f]      # journalctl -u nazman
#   sudo ./dev-live.sh restart   # restart the service
#   sudo ./dev-live.sh stop      # stop the service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="nazman"
PORT="8080"

usage() {
    sed -n '2,17p' "$0"
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: this command needs root. Try: sudo $0 $1" >&2
        exit 1
    fi
}

port_in_use() {
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$PORT($|\s)"
}

cmd_setup() {
    require_root setup

    if port_in_use; then
        echo "ERROR: port $PORT is already in use (is the dev server still running?)." >&2
        echo "  Stop it first (e.g. Ctrl-C on 'make dev'), then re-run setup." >&2
        exit 1
    fi

    if [[ -t 0 ]]; then
        echo "This runs the FULL production provisioning on this machine:"
        echo "  - apt-get upgrade (whole system)"
        echo "  - ZFS install (DKMS build on Raspberry Pi kernels)"
        echo "  - copy app to /opt/nazman, systemd service, firewall port $PORT"
        read -r -p "Continue? [y/N]: " _c
        [[ "$_c" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
    fi

    echo "==============================================="
    echo "dev-live.sh setup - installing system deps"
    echo "==============================================="
    bash "$SCRIPT_DIR/prepare.sh"

    echo "==============================================="
    echo "dev-live.sh setup - building the service"
    echo "==============================================="
    bash "$SCRIPT_DIR/build.sh"

    echo "==============================================="
    echo "dev-live.sh setup complete."
    echo "  app:        /opt/nazman"
    echo "  config:     /etc/nazman/nazman.conf"
    echo "  logs:       journalctl -u $SERVICE_NAME  (or ./dev-live.sh logs)"
    echo "  URL:        http://localhost:$PORT"
    echo ""
    echo "After editing code, re-apply with:  sudo ./dev-live.sh update"
    echo "To exercise the ZFS API, create a scratch pool, e.g. for loop devices:"
    echo "  sudo truncate -s 1G /var/lib/nazman/disk{1,2}.img"
    echo "  sudo zpool create testpool /var/lib/nazman/disk1.img /var/lib/nazman/disk2.img"
    echo "==============================================="
}

cmd_update() {
    require_root update
    bash "$SCRIPT_DIR/deploy.sh"
}

cmd_status() {
    systemctl status "$SERVICE_NAME" --no-pager || true
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:$PORT/" 2>/dev/null || echo '-')"
        echo "HTTP check: GET http://localhost:$PORT/ -> ${code:-unreachable}"
    fi
}

cmd_logs() {
    if [[ "${1:-}" == "-f" ]]; then
        journalctl -u "$SERVICE_NAME" -f
    else
        journalctl -u "$SERVICE_NAME" -n 200 --no-pager
    fi
}

cmd_restart() {
    require_root restart
    systemctl restart "$SERVICE_NAME"
    echo "Restarted $SERVICE_NAME."
}

cmd_stop() {
    require_root stop
    systemctl stop "$SERVICE_NAME"
    echo "Stopped $SERVICE_NAME."
}

ACTION="${1:-}"
case "$ACTION" in
    setup)   cmd_setup ;;
    update)  cmd_update ;;
    status)  cmd_status ;;
    logs)    cmd_logs "${2:-}" ;;
    restart) cmd_restart ;;
    stop)    cmd_stop ;;
    *)       usage ;;
esac