#!/bin/bash
# prepare.sh - Install system-level dependencies for NAZMan on Ubuntu Server.
#
# Run with sudo. Idempotent: calling it again brings installed packages up to
# date (apt upgrade). It only installs OS packages; it does NOT touch the
# application, /opt/nazman, or any NAZMan configuration.
#
# Usage: sudo ./prepare.sh

set -euo pipefail

echo "==============================================="
echo "NAZMan - prepare.sh (system dependencies)"
echo "==============================================="

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    exit 1
fi

echo "Updating package lists and upgrading installed packages..."
apt-get update -y
apt-get upgrade -y

echo "Installing system packages..."
apt-get install -y \
    zfsutils-linux \
    nfs-kernel-server \
    parted \
    gdisk \
    smartmontools \
    git \
    curl \
    wget \
    python3 \
    python3-venv \
    python3-pip \
    software-properties-common

# Extra pieces needed by the app's backing store logic:
#   util-linux provides lsblk/sfdisk/blkid/wipefs/partprobe/mount/umount
#   e2fsprogs provides mkfs.ext4
#   coreutils provides chown/chmod
apt-get install -y util-linux e2fsprogs coreutils

echo ""
echo "prepare.sh complete. Run: sudo ./build.sh"