# NAZMan - ZFS NAS Management System

A lightweight web-based management system for Ubuntu Server / Raspberry Pi OS that
provides a GUI for ZFS storage management with NFS v4 and SMB access.

> **User guide:** the full task-based user guide is served by the running app at
> `http://<server-ip>:8080/guide` (Overview, pools, datasets, shares, snapshots,
> backups, disk failure, dataset recovery, full-system recovery, troubleshooting).
> This README covers installing and developing NAZMan.

## Some background

This is really a learning exercise — learning about ZFS and about building a
deployable web app. The project is entirely vibe coded, using opencode and
largely with big-pickle, with some sanity checking from time to time with Claude.

It supports some functionality absent from the TrueNAS and Ugreen UIs — namely
the ability to use disk partitions rather than entire disks. This is to
facilitate home use where the number of disks may be limited. On a Ugreen
4800GT it will easily run a 2.5G LAN at over 80% utilisation using NFS given
the right configuration.

## Features

- ZFS pool management (create, import, export, scrub, destroy)
- Disk management by stable `by-id` identity; group-partitioning of multiple disks
- SLOG/L2ARC/Special vdev support
- Datasets with compression, quotas, record sizes
- NFS v4 exports and SMB shares (guest access through a shared anonymous user)
- Snapshots and configurable backup schedules
- Git-based configuration backup plus full/incremental ZFS data backup to external disks
- Dashboard monitoring with live and historical metrics

## Requirements

- Ubuntu Server 20.04+ (x86-64) or Raspberry Pi OS 64-bit (Trixie recommended) — any Debian-based distro
- Root access
- ZFS support (zfsutils-linux)
- Python 3.10+

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | **Production** artifact. App code, deployment scripts, and `install.sh` for a `curl \| bash` bootstrap. No `tests/` or dev tooling. |
| `dev`  | **Development** variant. Everything on `main` plus the test suite, `dev-env.sh`, a `Makefile`, an `.opencode/` config for opencode development, and CI. |

## Quick Start (Production)

```bash
curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | sudo bash
```

`install.sh` downloads the `main` branch into `/opt/nazman`, then `prepare.sh`
(system packages: ZFS, NFS, parted, smartmontools, ...) and `build.sh`
(directories, venv, default config, the shared `nfsanon` user, and the `nazman`
systemd service).

Then open **http://your-server-ip:8080** and sign in. A fresh install accepts
any password (no auth hash); set `AUTH_PASSWORD_HASH` in `/etc/nazman/nazman.conf`
to enforce one.

```bash
sudo systemctl status nazman      # service status
sudo journalctl -u nazman -f      # service logs
```

### Manual install / updates

```bash
cd /opt/nazman
sudo ./prepare.sh                 # apt: ZFS, NFS, python, etc.
sudo ./build.sh                   # one-off service setup
sudo ./deploy.sh                  # later: apply changed files + restart
```

`deploy.sh` uses `deploy.txt` as a manifest and applies only new/changed/deleted
files, then restarts the service. If `requirements.txt` changed, reinstall deps
with `/opt/nazman/venv/bin/pip install -r requirements.txt` (deploy.sh does not
pip install).

## Raspberry Pi OS

NAZMan runs on Raspberry Pi OS (64-bit). Use the **Trixie** release — it ships
Python 3.13 and kernel 6.12 (Bookworm only has Python 3.11).

| Role | Branch | Purpose | Notes |
|------|--------|---------|-------|
| **Dev machine** | `dev` | Run opencode, the test suite, and live-test the app | Unit tests mock ZFS; live testing runs the real production setup |
| **Backup service** | `main` | Receive/restore ZFS backup streams from the main NAS | Needs working ZFS |

On Raspberry Pi OS, `prepare.sh` enables the Debian `contrib` repository and
builds the ZFS kernel module via DKMS (a few minutes on first install). If the
DKMS build fails against a newly-shipped Pi kernel, `prepare.sh` prints manual
instructions for pulling newer ZFS from `trixie-backports`.

## Configuration

`/etc/nazman/nazman.conf`:

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

## Development

The `dev` branch is geared toward development (including with opencode). The
test suite has no ZFS dependency (ZFS commands are mocked):

```bash
git clone git@github.com:pootle/nazman.git
cd nazman && git checkout dev
./dev-env.sh                       # creates ./venv (python3.13 preferred)
./venv/bin/python -m pytest tests/ -q
```

To *live-test* the app exactly as production runs, use `dev-live.sh`. It
provisions the machine like a Pi: `prepare.sh` (apt system packages including
ZFS) then `build.sh` (app copied to `/opt/nazman`, venv there,
`/etc/nazman/nazman.conf`, the `nfsanon` identity, and the `nazman` systemd
service `Requires=zfs.target`):

```bash
sudo ./dev-live.sh setup           # one-time provision (installs ZFS)
./dev-live.sh status               # service status + HTTP check
sudo ./dev-live.sh update          # apply changed files + restart
./dev-live.sh logs -f              # journalctl -u nazman
```

Stop any local dev server (e.g. `make dev`, which binds 8080) before
`dev-live.sh setup`, or the service will not start. To exercise the ZFS API on
a ZFS-less dev box, create a scratch pool on loop devices:

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
├── docs/                # User guide pages (Markdown, served at /guide)
├── install.sh           # One-shot bootstrap (curl | bash)
├── prepare.sh           # System dependency installer (sudo)
├── build.sh             # Live service setup in /opt/nazman (sudo)
├── deploy.sh            # Update the running service (sudo)
├── dev-env.sh           # Local development/testing venv (dev branch)
└── tests/               # Test files (dev branch)
```

## API Documentation

Once the server is running, visit **http://your-server-ip:8080/docs** for the
interactive Swagger documentation of all API endpoints.

## Security Notes

- The application requires root access to manage ZFS/NFS
- Authentication is enabled by default
- Use a reverse proxy (nginx/caddy) for HTTPS
- Configure firewall to restrict access to port 8080