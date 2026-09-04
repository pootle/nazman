#!/bin/bash
# prepare.sh - Install system-level dependencies for NAZMan on Ubuntu Server
# or Raspberry Pi OS.
#
# Run with sudo. Idempotent: calling it again brings installed packages up to
# date (apt upgrade). It only installs OS packages; it does NOT touch the
# application, /opt/nazman, or any NAZMan configuration.
#
# Raspberry Pi OS notes:
#   - Debian-family ZFS lives in the 'contrib' component, which Pi OS repos do
#     not enable by default. This script enables it automatically.
#   - ZFS is built locally via DKMS, so linux-headers are required and the
#     first install compiles the module (a few minutes on a Pi 5).
#   - If the DKMS build fails against a newly-shipped Pi kernel, this script
#     prints manual instructions for pulling newer ZFS from trixie-backports.
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

# Run apt non-interactively: the zfs-dkms install pops a license prompt that
# cannot be answered in a piped/scripted context (no interactive TTY).
export DEBIAN_FRONTEND=noninteractive

# Determine the platform: Ubuntu (deb.debian.org repos enable contrib) vs
# Raspberry Pi OS (Debian-based; contrib disabled by default).
IS_RPI=0
if [[ -f /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
    IS_RPI=1
fi

# Enable the Debian 'contrib' component so ZFS packages are installable. Handles
# both the legacy /etc/apt/sources.list and the deb822 *.sources format used by
# Raspberry Pi OS Trixie.
enable_contrib() {
    local changed=0

    # deb822 format: /etc/apt/sources.list.d/*.sources with a Components: line.
    # Skip Pi-specific repos (archive.raspberrypi.com) which don't carry contrib.
    for f in /etc/apt/sources.list.d/*.sources; do
        [[ -e "$f" ]] || continue
        if grep -q "archive.raspberrypi.com" "$f"; then
            continue
        fi
        if ! grep -q "^Components:.* contrib" "$f"; then
            sed -i -E 's/^(Components:\s*main[^#]*)$/\1 contrib /' "$f"
            changed=1
        fi
    done

    # Legacy format: lines like "deb <uri> <suite> main [contrib]".
    # Only touch lines whose URI is NOT a Pi-specific repo.
    if [[ -f /etc/apt/sources.list ]]; then
        if grep -q "^deb " /etc/apt/sources.list && \
           ! grep -q "contrib" /etc/apt/sources.list; then
            sed -i -E '/archive\.raspberrypi\.com/!s/^(deb\s+\S+\s+\S+\s+main)([^#]*)$/\1 contrib \2/' /etc/apt/sources.list
            changed=1
        fi
    fi

    return $changed
}

if [[ "$IS_RPI" -eq 1 ]]; then
    echo "Detected Raspberry Pi OS. Ensuring the 'contrib' repository is enabled (needed for ZFS)..."
    if enable_contrib; then
        echo "Added 'contrib' to apt sources; refreshing package lists."
        apt-get update -y
    else
        echo "'contrib' already enabled."
    fi
fi

echo "Updating package lists and upgrading installed packages..."
apt-get update -y
apt-get upgrade -y

# On Debian-family (Raspberry Pi OS), ZFS requires DKMS source plus the kernel
# headers to build the module locally. Install them explicitly before the
# zfsutils metapackage so apt resolves everything in one pass.
RPI_ZFS_PKGS=""
if [[ "$IS_RPI" -eq 1 ]]; then
    KERN_REL="$(uname -r)"
    RPI_ZFS_PKGS="linux-headers-${KERN_REL} zfs-dkms zfs-zed"
fi

# Pre-accept the zfs-dkms licenses note to avoid a blocking debconf prompt
# (the postinst pops a "note" dialog that cannot be answered without a TTY).
echo "zfs-dkms zfs-dkms/note-incompatible-licenses note true" | debconf-set-selections || true

echo "Installing system packages..."
apt-get install -y \
    ${RPI_ZFS_PKGS:+$RPI_ZFS_PKGS} \
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
    python3-pip

# Extra pieces needed by the app's backing store logic:
#   util-linux provides lsblk/sfdisk/blkid/wipefs/partprobe/mount/umount
#   e2fsprogs provides mkfs.ext4
#   coreutils provides chown/chmod
apt-get install -y util-linux e2fsprogs coreutils

# ── NFSv4 availability check ───────────────────────────────────────────
# NAZMan's NFS sharing uses ZFS sharenfs, which relies on the kernel NFS
# server.  Ensure the nfsd module is loaded and the proc filesystem is
# accessible so that NFSv4 is available to clients.
echo "Checking NFSv4 support..."
if ! lsmod | grep -q nfsd; then
    modprobe nfsd 2>/dev/null || true
fi
if [[ -d /proc/fs/nfsd ]]; then
    echo "NFSv4 support is available (/proc/fs/nfsd present)."
else
    echo ""
    echo "==============================================="
    echo "WARNING: NFSv4 support is NOT available." >&2
    echo "" >&2
    echo "The /proc/fs/nfsd directory is missing, which means the nfsd" >&2
    echo "kernel module is not loaded.  This can happen on minimal installs" >&2
    echo "or if the kernel was updated without rebooting." >&2
    echo "" >&2
    echo "To fix:" >&2
    echo "  sudo modprobe nfsd" >&2
    echo "  sudo systemctl restart nfs-kernel-server" >&2
    echo "" >&2
    echo "If modprobe fails, ensure nfs-kernel-server is installed and" >&2
    echo "reboot into the running kernel." >&2
    echo "===============================================" >&2
    echo ""
fi

# Post-install ZFS sanity check (DKMS builds the module; newly-shipped Pi
# kernels can break the build). If the module isn't functional, report the
# failure and give manual remediation rather than silently proceeding to build.sh.
if [[ "$IS_RPI" -eq 1 ]]; then
    if ! zpool version &>/dev/null; then
        echo ""
        echo "==============================================="
        echo "WARNING: ZFS did not become usable after install." >&2
        echo "" >&2
        echo "The ZFS kernel module is built locally via DKMS. If the current" >&2
        echo "Raspberry Pi kernel is newer than what the packaged ZFS supports," >&2
        echo "the build fails. To retry with newer ZFS from trixie-backports:" >&2
        echo "" >&2
        echo "  sudo apt install -t trixie-backports zfs-dkms zfsutils-linux zfs-zed" >&2
        echo "" >&2
        echo "Then rebuild the module (use the version shown by 'dkms status'):" >&2
        echo "  sudo dkms status" >&2
        echo "  sudo dkms build zfs/<version> -k \$(uname -r)" >&2
        echo "  sudo modprobe zfs && zpool version" >&2
        echo "" >&2
        echo "NAZMan depends on ZFS (zfs.target); build.sh will not start the" >&2
        echo "service until this is resolved." >&2
        echo "===============================================" >&2
    fi
fi

echo ""
echo "prepare.sh complete. Run: sudo ./build.sh"
