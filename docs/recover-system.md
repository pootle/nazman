# Recover a Complete System

The disk died, the OS was reinstalled, or the whole host was replaced. Goal:
get back to exactly where you were — configuration, pools, datasets, and shares.

## What you have to work with

Hot tip: everything you need was created during normal operation.

- **Configuration backup** — git repo on this server (path shown on the
  **Backup** page, `backup_repo_path` in the config).
- **Data backups** — external backup disks declared on the [Backup](backup)
  page, containing gzip'd ZFS streams.
- **The pools' disks themselves** — if the pool was created from multiple
  disks that survived (e.g. mirror/RAIDZ), the pool can be imported with all
  its data.

## Step 1 — Reinstall and restore the OS

1. Install the OS (Ubuntu Server or Raspberry Pi OS 64-bit).
2. Install NAZMan with the one-liner:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/pootle/nazman/main/install.sh | sudo bash
   ```
   or run `prepare.sh` then `build.sh` from a checkout.
3. The web UI is now on `http://<server-ip>:8080`. Sign in (an empty-password
   fresh install accepts anything; set `AUTH_PASSWORD_HASH` in
   `/etc/nazman/nazman.conf` to lock it down).

## Step 2 — Import the pools

If the old data disks are still in the machine, import the pool rather than
recreating:

```bash
sudo zpool import -a        # import all exported/unattached pools
zpool status                # ONLINE?
```

> **Info:** If the pool was *exported* cleanly before the failure it imports

> without further action. After an unclean shutdown ZFS may ask for
> `zpool import <pool>` with `-F` (force) to roll back the tail of the log —
> allow it.

If a data disk died with the system (restoring the OS disk, for example), just
**create a fresh pool** for the new data now — see [Set Up a Pool](pools) —
and restore datasets into it from backup in step 4.

## Step 3 — Restore the configuration

On the **Backup** page:

1. If **Repository:** reads **Not Initialized**, the `backup_repo_path` setting
   points at a new location — point `/etc/nazman/nazman.conf` at the old repo
   (e.g. the OS disk backup / the same external disk) and reload.
2. In **Restore Configuration**, pick the last good commit, click **Restore**
   and confirm **twice**.
3. Prefer the **most recent commit with few uncommitted changes** — click
   **Backup History** to list them, then restore the one before the crash.

The configuration holds the dataset/share definitions and backup layout, so
the UI now shows your familiar pools and shares.

## Step 4 — Recreate datasets and restore data

1. Create the pool(s) — again see [Set Up a Pool](pools).
2. Create the datasets you had — see [Create a Dataset](datasets).
3. Re-declare the external backup disks — plug each one in, then
   **Declare New Backup Disk** (use the same labels; the disk's UUID must match
   to show **Mounted**).
4. Restore each dataset from the mounted disks — **Restore from Backup Disk**:
   pick the disk, pick the stream file, type the dataset name, **Restore**.
   See [Recover a Dataset](recover-dataset).

## Step 5 — Re-create shares

Re-share the restored datasets over NFS and SMB — see
[Share Data](shares). The config restore in step 3 *recorded* these settings;
the running shares themselves are re-exported when you recreate/save each one.

## Step 6 — Restart the backup cadence

- Set the **Full/Incr cron** schedules again on the **Datasets to Back Up**
  card.
- Trigger a **Backup Now** on the configuration backup so your restore point
  exists.
- Run a scrub after a few weeks to confirm the recovered pool is healthy.

> **Summary:** OS back → pools imported → config restored → datasets back from
> external backup disks → shares recreated → backups resumed. A full recovery is
> possible from the external disks alone; the pool itself can only help if at
> least one original vdev survived.

Next: [Troubleshooting](troubleshooting)