# NAZMan User Guide

NAZMan is the web management interface for this ZFS NAS. It runs on the server
itself — open `http://<server-ip>:8080` in a browser and sign in with your
password.

This guide walks through the whole lifecycle of a NAS in reading order: storage,
data, sharing, protection, and — when something goes wrong — recovery.

## What you can do

| Page | Purpose |
|---|---|
| **Dashboard** | Live CPU/memory/network, per-pool usage, pool health at a glance, recent events |
| **Disks** | See every attached disk, check its SMART health, mark dead disks, partition drives |
| **Pools** | Create, scrub, export and import ZFS pools; add cache/log/special vdevs |
| **Datasets** | Create and configure ZFS datasets (compression, quotas, record size) |
| **NFS / SMB** | Share datasets over the network, restrict clients, pause/remove shares |
| **Snapshots** | Take and destroy point-in-time snapshots of datasets |
| **Backup** | Full/incremental dataset backups to external disks, with the configuration captured on each volume |
| **Restore** | Restore configuration/datasets and rebuild a complete system from the backup disks |
| **Command Log** | Every command NAZMan runs, with success/failure status (useful when diagnosing) |
| **Settings** | System information card |

## First-time setup order

These pages are covered in detail next:

1. [Set up a pool](pools) — partition spare disks, create a pool with the right
   redundancy for your data.
2. [Create a dataset](datasets) — carve storage out of the pool with compression
   and quotas.
3. [Share data](shares) — expose datasets over NFS and/or SMB.
4. [Set up backups](backup) — declare an external backup disk (it is seeded
   with the configuration automatically) and schedule dataset backups. Use
   [Restore](restore) to recover or rebuild.

## Reading this guide

- Steps marked with **DESTRUCTIVE** headings cannot be undone.
- Defaults in screenshots are the recommended values — the forms pre-fill them.
- Where NAZMan deliberately leaves a task to the command line (for example
  replacing a failed zpool device), the exact commands are shown in a fenced
  block. NAZMan runs as **root**, so log in with `sudo -i` first.

> **Warning:** ZFS is the source of truth for pools, datasets and shares. If

> you make a change underneath NAZMan (e.g. `zpool create` at a shell), refresh
> the page or hit the Refresh button before continuing.

Next: [Set up a Pool](pools)