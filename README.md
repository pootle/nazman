# NAZMan - ZFS NAS Management System

A lightweight web-based management system for Ubuntu Server / Raspberry Pi OS that
provides a GUI for ZFS storage management with NFS v4 and SMB access.

## Some background

This is really a learning exercise — learning about ZFS and about building a
deployable web app.

The project is entirely vibe coded, using opencode and largely with big-pickle,
with some sanity checking from time to time with Claude.

It does support some functionality absent from the TrueNAS and Ugreen UIs —
namely the ability to use disk partitions rather than entire disks. This is to
facilitate home use where the number of disks may be limited.

I have tested this, and on a Ugreen 4800GT it will easily run a 2.5G LAN at
over 80% utilisation using NFS given the right configuration.

## Features

- **ZFS Pool Management**: Create, import, export, and monitor ZFS pools
- **Disk Management**: Discover, group, and partition disks with identical layouts
- **SLOG/L2ARC Support**: Add write and read cache devices to pools
- **Dataset Management**: Create and configure ZFS datasets with properties
- **NFS v4 Exports**: Manage NFS exports with client access control
- **SMB Shares**: Manage Samba shares (via smb.conf)
- **Snapshot Management**: Create, list, and destroy snapshots
- **Configuration Backup**: Git-based versioned backup of all configurations
- **ZFS Data Backup**: Send/receive backups of pools to external disks
- **Scheduled Tasks**: Automate scrubs, snapshots, and health checks
- **Monitoring**: Real-time and historical CPU/memory/disk/network metrics

## Requirements

- Ubuntu Server 20.04+ (x86-64) or Raspberry Pi OS 64-bit (Trixie recommended) — any Debian-based distro
- Root access
- ZFS support (zfsutils-linux)
- Python 3.9+

## Branches

This repository contains two branches:

| Branch | Purpose |
|--------|---------|
| `main` | **Production** artifact. App code, deployment scripts, and `install.sh` for a `curl \| bash` bootstrap. No `tests/` or dev tooling. |
| `dev`  | **Development** variant. Everything on `main` plus the test suite, `dev-env.sh`, a `Makefile`, an `.opencode/` config for opencode development, and CI. |

## Quick Start (Production)

The recommended way to install NAZMan on a fresh server is a one-liner. On Ubuntu
Server or Raspberry Pi OS, with root:

```bash
curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | sudo bash
```

`install.sh` downloads the `main` branch into `/opt/nazman`, then:

1. Runs `prepare.sh` — installs system packages (ZFS, NFS, parted, smartmontools, etc.)
2. Runs `build.sh` — creates directories, a Python venv, writes the default config,
   creates the shared `nfsanon` user, and installs/starts the `nazman` systemd service.

After it completes, the service runs under systemd:

```bash
sudo systemctl status nazman
```

### Managing from the source

If you prefer a manual install from a checkout (e.g. on the Pi):

```bash
cd /opt/nazman

# 1. Install system dependencies (apt: ZFS, NFS, python, etc.)
sudo ./prepare.sh

# 2. Set up /opt/nazman as a service and start it (one-off)
sudo ./build.sh

# 3. Later, after code changes or an update (e.g. git pull):
sudo ./deploy.sh
```

`deploy.sh` uses `deploy.txt` as a manifest and applies only new/changed/deleted
files, then restarts the service.

## Raspberry Pi OS

NAZMan runs on Raspberry Pi OS (64-bit). Typical roles for a Pi:

| Role | Branch | Purpose | Notes |
|------|--------|---------|-------|
| **Dev machine** | `dev` | Run opencode, the test suite, and live-test the app | Unit tests mock ZFS; live testing runs the real production setup |
| **Backup service** | `main` | Receive/restore ZFS backup streams from the main NAS | Needs working ZFS |

Use the **Trixie** release of Raspberry Pi OS (64-bit) — it ships Python 3.13
and kernel 6.12 (the older Bookworm release only has Python 3.11).

### Dev machine

The test suite has no ZFS dependency (ZFS commands are mocked):

```bash
# Install opencode (native arm64 binary)
curl -fsSL https://opencode.ai/install | bash

# Install git if not present
sudo apt-get install -y git

# Configure your Git identity (required for commits)
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# Clone and set up the dev branch
git clone git@github.com:pootle/nazman.git
cd nazman
git checkout dev

# Create the venv and run the test suite
./dev-env.sh
./venv/bin/python -m pytest tests/ -q
```

To *live-test* the app on the dev machine in the same way production runs, use
`dev-live.sh`. It provisions the machine exactly like the Pi: `prepare.sh`
(apt system packages including ZFS) then `build.sh` (app copied to
`/opt/nazman`, venv there, `/etc/nazman/nazman.conf`, the `nfsanon` identity,
and the `nazman` systemd service `Requires=zfs.target`):

```bash
# One-time provision (sudo; installs ZFS and upgrades apt packages)
sudo ./dev-live.sh setup

# Check the service and endpoint
./dev-live.sh status

# After editing code, re-apply only changed files and restart
sudo ./dev-live.sh update

# Service logs
./dev-live.sh logs -f
```

Stop any local dev server (e.g. `make dev`, which binds 8080) before
`dev-live.sh setup`, or the service will not start. The fresh install accepts
any password at the login prompt (no auth hash set, like a fresh Pi); use
`set_setting`/`AUTH_PASSWORD_HASH` in `/etc/nazman/nazman.conf` to enforce one.
To exercise the ZFS API on a ZFS-less dev box, create a scratch pool on loop
devices, for example:

```bash
sudo truncate -s 1G /var/lib/nazman/disk{1,2}.img
sudo zpool create testpool /var/lib/nazman/disk1.img /var/lib/nazman/disk2.img
```

### Backup / production service

Install the service with the standard one-liner (requires a working ZFS). On
Raspberry Pi OS, `prepare.sh` automatically enables the Debian `contrib`
repository and builds the ZFS kernel module via DKMS, which takes a few minutes
on first install:

```bash
curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | sudo bash
```

If the DKMS build fails against a newly-shipped Pi kernel, `prepare.sh` prints
manual instructions for pulling newer ZFS from `trixie-backports`.

### Access the Web Interface

```
http://your-server-ip:8080
```

The first-run config sets `AUTH_ENABLED = true` with an empty password hash; set
`AUTH_PASSWORD_HASH` in `/etc/nazman/nazman.conf` (bcrypt) to enable login.

## Configuration

The main configuration file is `/etc/nazman/nazman.conf`:

```ini
DATABASE_PATH = /var/lib/nazman/nazman.db
BACKUP_ENABLED = true
BACKUP_REPO_PATH = /mnt/backup/nazman-config
AUTH_ENABLED = true
AUTH_PASSWORD_HASH =
MONITORING_REFRESH_INTERVAL = 5
LOGGING_LEVEL = INFO
APP_HOST = 0.0.0.0
APP_PORT = 8080
```

## Usage

### Creating a Pool

1. Go to **Disks** page and create a disk group
2. Partition the disk group with your desired layout
3. Go to **Pools** page and create a new pool
4. Select the topology (mirror, RAIDZ, etc.) and devices

### Adding SLOG/L2ARC

1. Go to **Pools** page
2. Select the pool and enter device paths
3. Click "Add SLOG" or "Add L2ARC"

### Creating NFS Exports

1. Go to **NFS** page
2. Click "Create Export"
3. Select a dataset and configure client access

### Backup System

NAZMan automatically backs up configuration to a Git repository. You can view
backup history, create manual backups, and restore from previous backups.

## Architecture

- **Backend**: FastAPI (Python)
- **Frontend**: Vanilla HTML/CSS/JavaScript
- **Database**: SQLite with WAL mode
- **ZFS Interface**: Subprocess wrappers around zpool/zfs CLI
- **Backup**: Git-based versioning

## Development

The `dev` branch is geared toward development (including with opencode). Make
sure `git` is installed and your credentials are configured, then:

```bash
# Install git if not present (Debian/Ubuntu)
sudo apt-get install -y git

# Configure your Git identity (required for commits)
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# Clone and switch to the dev branch
git clone git@github.com:pootle/nazman.git
cd nazman
git checkout dev

# Create a local dev venv (prefers python3.13, falls back to any Python 3.9+)
./dev-env.sh

# Or, if you have a Makefile toolchain:
make setup

# Run the test suite (ZFS commands are mocked)
./venv/bin/python -m pytest tests/ -q
```

To *live-test* the app on the dev machine in the same way production runs, use
`dev-live.sh`. It provisions the machine exactly like a Pi: `prepare.sh`
(apt system packages including ZFS) then `build.sh` (app copied to
`/opt/nazman`, venv there, `/etc/nazman/nazman.conf`, the `nfsanon` identity,
and the `nazman` systemd service `Requires=zfs.target`):

```bash
# One-time provision (sudo; installs ZFS and upgrades apt packages)
sudo ./dev-live.sh setup

# Check the service and endpoint
./dev-live.sh status

# After editing code, re-apply only changed files and restart
sudo ./dev-live.sh update

# Service logs
./dev-live.sh logs -f
```

Stop any local dev server (e.g. `make dev`, which binds 8080) before
`dev-live.sh setup`, or the service will not start. The fresh install accepts
any password at the login prompt (no auth hash set, like a fresh Pi); use
`set_setting`/`AUTH_PASSWORD_HASH` in `/etc/nazman/nazman.conf` to enforce one.
To exercise the ZFS API on a ZFS-less dev box, create a scratch pool on loop
devices, for example:

```bash
sudo truncate -s 1G /var/lib/nazman/disk{1,2}.img
sudo zpool create testpool /var/lib/nazman/disk1.img /var/lib/nazman/disk2.img
```

### Project Structure

```
nazman/
├── nazman/              # Main application package
│   ├── api/             # API endpoints
│   ├── managers/        # Business logic
│   ├── models/          # Database models
│   └── utils/           # Utilities
├── static/              # Static files (CSS, JS)
├── templates/           # HTML templates
├── install.sh           # One-shot bootstrap (curl | bash)
├── prepare.sh           # System dependency installer (sudo)
├── build.sh             # Live service setup in /opt/nazman (sudo)
├── deploy.sh            # Update the running service (sudo)
├── dev-env.sh           # Local development/testing venv (dev branch)
└── tests/               # Test files (dev branch)
```

## API Documentation

Once the server is running, visit:
```
http://your-server-ip:8080/docs
```

This provides interactive Swagger documentation for all API endpoints.

## Security Notes

- The application requires root access to manage ZFS/NFS
- Authentication is enabled by default
- Use a reverse proxy (nginx/caddy) for HTTPS
- Configure firewall to restrict access to port 8080
