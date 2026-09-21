# Recover a Complete System

The disk died, the OS was reinstalled, or the whole host was replaced. Goal:
get back to exactly where you were — configuration, pools, datasets, and shares.

## What you have to work with

Hot tip: everything you need was created during normal operation.

- **Configuration bundles** — stored on every backup volume (database + host
  config + pool/partition exports).
- **Data backups** — gzip'd, checksummed ZFS streams on your external backup
  disks, described by a self-describing manifest.
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

## Step 2 — Import surviving pools (if any)

If the old data disks are still in the machine, import the pool rather than
recreating it:

```bash
sudo zpool import -a        # import all exported/unattached pools
zpool status                # ONLINE?
```

> **Info:** If the pool was *exported* cleanly before the failure it imports

> without further action. After an unclean shutdown ZFS may ask for
> `zpool import <pool>` with `-F` (force) to roll back the tail of the log —
> allow it.

If the pools are gone (new disks, or all members replaced), recreate them from
the backup manifest in step 3.

## Step 3 — Rebuild from a backup disk

Open the **Restore** page and use the **Rebuild This System** wizard — see
[Restore & Rebuild](restore) for the full walkthrough:

1. **Scan disks** to find the backup info set on your backup volume.
2. **Review** the recorded pools, vdevs, datasets and configuration.
3. **Recreate pools** — assign attached disks to each recorded vdev slot and
   create each pool (or skip pools you imported in step 2). Pools on partitions
   use **Prepare partitions** to reproduce the recorded slot layout first.
4. **Restore datasets** — choose target pools and restore; insert each backup
   disk when prompted.
5. **Finish** — adopt the backup media and rebuild schedules.

You can also **Restore Configuration** from any volume's bundle at any point to
bring back shares and settings.

## Step 4 — Re-create shares

Restoring the configuration records your NFS/SMB settings. Re-share the
restored datasets over NFS and SMB — see [Share Data](shares). The running
shares themselves are re-exported when you recreate/save each one.

## Step 5 — Confirm and resume

- Set the **Full/Incr cron** schedules again on the **Backup** page if you did
  not rebuild them.
- Run a dataset backup so a fresh restore point exists (the configuration is
  captured on the volume automatically).
- Run a scrub after a few weeks to confirm the recovered pool is healthy.

> **Summary:** OS back → pools imported or recreated from the manifest →
> datasets restored from the backup disks → configuration restored → shares
> recreated → backups resumed. A full recovery is possible from the external
> disks alone; surviving pool disks only save the restore step.

Next: [Troubleshooting](troubleshooting)
