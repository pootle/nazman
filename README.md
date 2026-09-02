# NAZMan - ZFS NAS Management System

A lightweight web-based management system for Ubuntu Server / Raspberry Pi OS that
provides a GUI for ZFS storage management with NFS v4 and SMB access.

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

- Ubuntu Server 20.04+ or Raspberry Pi OS (Bookworm/Linux) — any Debian-based distro
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
sudo curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | bash
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

The `dev` branch is geared toward development (including with opencode). Switch
branches, then:

```bash
git checkout dev

# Create a local dev venv (python3.13) and install deps + test tooling
./dev-env.sh

# Or, if you have a Makefile toolchain:
make setup

# Run the test suite
./venv/bin/python -m pytest tests/ -q

# Run the development server (auto-reload)
./venv/bin/uvicorn nazman.main:app --reload --host 0.0.0.0 --port 8080
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
