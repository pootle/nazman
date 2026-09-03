#!/bin/bash
# build.sh - One-time setup of the live NAZMan service in /opt/nazman.
#
# Run with sudo on a fresh (or existing) Ubuntu Server. This script:
#   0. Prompts to install network-sharing packages (nfs-kernel-server, samba).
#   1. Creates the NAZMan directories (/opt/nazman, /var/lib/nazman,
#      /var/log/nazman, /etc/nazman).
#   2. Copies the application into /opt/nazman (per deploy.txt).
#   3. Creates the Python venv (/opt/nazman/venv) using python3.13 and
#      installs Python dependencies.
#   4. Writes the default config (/etc/nazman/nazman.conf).
#   5. Creates the shared anonymous user/group (nfsanon, UID/GID 65533), used
#      by both NFS and SMB for consistent read/write access.
#   6. Installs the systemd service and starts it.
#
# Intended to be run once; use ./deploy.sh afterwards to apply updates.
#
# Usage: sudo ./build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SCRIPT_DIR/deploy.txt"
DEST="/opt/nazman"
SERVICE_NAME="nazman"

# Python used for the NAZMan venv. Any modern python3.x works; current releases
# of pydantic/pydantic-settings fully support Python 3.14, so we default to the
# system python3 instead of pinning an older minor.
PY_BIN="python3"

echo "==============================================="
echo "NAZMan - build.sh (live service setup)"
echo "==============================================="

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    exit 1
fi

if [[ ! -f "$MANIFEST" ]]; then
    echo "ERROR: $MANIFEST not found." >&2
    exit 1
fi

# Resolve a usable Python interpreter: prefer an existing functional venv so
# build.sh is idempotent, otherwise use the system python3.
if [[ -x "$DEST/venv/bin/python" ]] && "$DEST/venv/bin/python" -c 'import sys; assert sys.version_info >= (3, 9)' &>/dev/null; then
    echo "Using existing venv at $DEST/venv ($("$DEST/venv/bin/python" --version 2>&1))."
elif command -v "$PY_BIN" &>/dev/null; then
    echo "Using $PY_BIN from system PATH ($("$PY_BIN" --version 2>&1))."
else
    echo "ERROR: no usable Python found. Run ./prepare.sh first (or install python3)." >&2
    exit 1
fi

# Offline premise: apt is non-interactive during scripted installs.
export DEBIAN_FRONTEND=noninteractive

echo ""
echo "--------------------------------------------------"
echo "Network sharing services to install on this server:"
echo ""

install_nfs="n"
install_smb="n"

if [[ -t 0 ]]; then
    echo "NFS exports are served by the nfs-kernel-server package."
    read -r -p "Install NFS server (nfs-kernel-server)? [y/N]: " _n
    [[ "$_n" =~ ^[Yy]$ ]] && install_nfs="y"

    echo "SMB shares are served by the samba package."
    read -r -p "Install Samba (samba)? [y/N]: " _s
    [[ "$_s" =~ ^[Yy]$ ]] && install_smb="y"
else
    echo "Non-interactive shell: skipping install prompts. You can install later with:"
    echo "  sudo apt-get install -y nfs-kernel-server   (NFS)"
    echo "  sudo apt-get install -y samba               (SMB)"
fi

if [[ "$install_nfs" == "y" || "$install_smb" == "y" ]]; then
    echo "1/7 Refreshing package lists (apt-get update)..."
    apt-get update
fi

if [[ "$install_nfs" == "y" ]]; then
    echo "Installing NFS kernel server..."
    apt-get install -y nfs-kernel-server
    systemctl enable nfs-kernel-server
    systemctl start nfs-kernel-server
fi

if [[ "$install_smb" == "y" ]]; then
    echo "Installing Samba..."
    apt-get install -y samba
    systemctl enable smbd nmbd
    systemctl start smbd nmbd
fi

echo "2/7 Creating directories..."
mkdir -p "$DEST" /var/lib/nazman /var/log/nazman /etc/nazman

echo "3/7 Copying application files to $DEST..."
# When build.sh runs from within $DEST itself (as install.sh does after cloning
# the repo into /opt/nazman), source and destination are the same directory, so
# there is nothing to copy.
if [[ "$(cd "$SCRIPT_DIR" && pwd -P)" == "$(cd "$DEST" && pwd -P)" ]]; then
    echo "  source is $DEST itself; skipping copy."
else
    while IFS= read -r file; do
        [[ -z "$file" || "$file" =~ ^# ]] && continue
        src="$SCRIPT_DIR/$file"
        if [[ -f "$src" ]]; then
            mkdir -p "$(dirname "$DEST/$file")"
            cp "$src" "$DEST/$file"
        fi
    done < "$MANIFEST"
fi

echo "4/7 Setting up Python virtual environment ($PY_BIN)..."
if [[ ! -x "$DEST/venv/bin/python" ]]; then
    "$PY_BIN" -m venv "$DEST/venv"
fi
"$DEST/venv/bin/pip" install --upgrade pip
"$DEST/venv/bin/pip" install -r "$DEST/requirements.txt"

echo "5/7 Writing default configuration (/etc/nazman/nazman.conf)..."
cat > /etc/nazman/nazman.conf << 'EOF'
# NAZMan configuration (pydantic-settings, flat key = value format)

# Database
DATABASE_PATH = /var/lib/nazman/nazman.db

# Backup
BACKUP_ENABLED = true
BACKUP_REPO_PATH = /mnt/backup/nazman-config
BACKUP_AUTO_COMMIT = true
BACKUP_PUSH_ON_COMMIT = true

# Auth
AUTH_ENABLED = true
AUTH_PASSWORD_HASH =

# Monitoring
MONITORING_REFRESH_INTERVAL = 5
MONITORING_ENABLE_WEBSOCKET = true

# Logging
LOGGING_LEVEL = INFO
LOGGING_FILE = /var/log/nazman/nazman.log

# App
APP_HOST = 0.0.0.0
APP_PORT = 8080
EOF

echo "6/7 Creating shared anonymous user/group (nfsanon, 65533) for NFS & SMB..."
if ! getent group nfsanon &>/dev/null; then
    groupadd -g 65533 nfsanon
fi
if ! getent passwd nfsanon &>/dev/null; then
    useradd -r -g 65533 -u 65533 -M -s /usr/sbin/nologin -d /var/lib/nfs nfsanon
fi

echo "7/7 Installing systemd service..."
cat > /etc/systemd/system/nazman.service << 'EOF'
[Unit]
Description=NAZMan - ZFS NAS Management
After=network.target zfs.target
Requires=zfs.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/nazman
ExecStart=/opt/nazman/venv/bin/uvicorn nazman.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "nazman.service"
systemctl start "nazman.service"

echo "Opening firewall port 8080..."
ufw allow 8080/tcp >/dev/null 2>&1 || true

echo ""
echo "==============================================="
echo "build.sh complete. Access at:"
echo "  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
echo ""
echo "Use ./deploy.sh after code changes; use ./dev-env.sh for a local dev venv."
echo "==============================================="