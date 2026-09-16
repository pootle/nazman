# Set Up Backups

NAZMan protects you at two levels:

1. **Configuration backup** — the app's own settings, stored in a git repo.
2. **Data backup** — full and incremental streams of your datasets, sent to a
   declared **external backup disk**.

Both live on the **Backup** page (titled *Configuration Backup*).

## 1. Configuration backup (git)

### Enable it

The repository lives at a path set in the server config (`backup_repo_path`).
On this page, the **Backup Status** card shows **Repository:** **Initialized**
or **Not Initialized**, the **Path:**, and **Last Backup:** (**Never** before
the first).

- Click **Backup Now** to make a manual commit now (optional commit message).
- **Uncommitted Changes:** **Yes** means settings changed since the last
  commit — click **Backup Now** to capture them.

### History and restore

- **Backup History** lists commits: **Commit** (8-char hash), **Message**,
  **Files Changed**, **Date**.
- **Restore Configuration** rolls the app's configuration back to a previous
  commit: pick the backup in **Select Backup:**, click **Restore**, and confirm
  *twice* (it overwrites the current configuration).

> **Warning:** The configuration backup stores *settings* (and which datasets

> are shared/backed up), **not** the pools' data. Restoring it does not touch
> your files.

## 2. Data backup — declare a backup disk

Data backups go to **external disks** you plug in (USB disks, or a removable
enclosure). The disk is formatted `ext4`; single- or multi-partition disks are
both supported.

Open the **Backup** page and scroll to **ZFS Data Backup**. In the
**Backup Disks** card click **Declare New Backup Disk**.

1. **Label** — something you can see on the drive, e.g. `Media backup 1`.
2. Pick the target in the device picker:
   - a **whole disk** (tagged *whole disk*), or
   - one **partition** (`<device> p<number>`, shown with its slot UUID).
3. Click **Declare**, then read the **Confirm Format** dialog — it states the
   exact target:
   - whole disk: *"This will destroy ALL partitions and data on the ENTIRE disk
     and format it."*
   - partition: *"The selected partition will be wiped and formatted."*
4. If software RAID metadata is detected, the dialog shows the array details
   and a **confirm DESTROY RAID metadata on this disk** checkbox (checked by
   default) — NAZMan will stop the array and wipe its metadata.
5. Click **Wipe & Format**. On success the disk is declared, formatted, mounted
   briefly to record its filesystem UUID, and set as the backup target.

> **Danger:** Declaring a whole disk erases every partition on it. Label the

> physical drive — there is no way to tell disks apart afterwards except the
> label you entered.

### Backup disk states

Each declared disk shows a **Status** badge:

| Status | Means |
|---|---|
| **Mounted** | Plugged in and mounted |
| **Unmounted** | Plugged in, not mounted |
| **Full** | Mounted but the disk is full |
| **Not connected** | No device detected — try **Wake / Replug**; if the enclosure is fitted but powered off, power-cycle it |
| **Filesystem changed** | A device is present but its UUID differs from what was declared — re-scan or re-declare |

**Capacity** and **Free** are computed live, so plug in the disk to see
accurate numbers.

### Actions per disk

- **Mount / Unmount** — prepare the disk for a backup or put it away.
- **Wake / Replug** — force a sleeping or dropped USB disk to re-enumerate
  (software reset, no power cycle). Shown for **Not connected** disks.
- **Scan** — re-probe the disk and refresh status/capacity.
- **Unmount after backup** checkbox — if ticked, NAZMan unmounts the disk after
  each backup/restore so it can be unplugged safely.
- **Remove** — deregisters the disk from NAZMan. Files on the disk are
  preserved; you can re-declare it later.

## 3. Schedule and run backups

In the **Datasets to Back Up** card, each dataset lists its assigned backup
disks. Per (dataset, disk) block:

- **Full cron** and **Incr cron** fields — cron expressions such as
  `0 2 * * 0` (full on Sundays 02:00) and `0 3 * * *` (incremental at 03:00
  daily). Click **Save** to apply.
- **Full** — run a full backup right now (snapshot-stream, resets the
  incremental chain).
- **Incr** — run an incremental backup; only changes since the last anchor are
  sent. NAZMan auto-promotes to full when no anchor snapshot exists.
- **Remove** — delete this dataset's schedule on that disk; existing runs are
  kept.
- **Add another disk** — assign an additional disk for
  grandfather-father-son tiers (same dataset on several disks, different
  schedules).

The status badge per block shows **running…**, **last OK**, **last failed**, or
**no runs yet**. **Changed since full** shows how much has changed since the
last anchoring full, and **Last backup** shows the latest run.

> **Note:** **/data is mounted** — freshly declared disks that were formatted
> whole-unmounted first make no changes until you run a backup or mount them.

## 4. Backup runs and restore

The **Backup Runs** card lists every run — **Dataset**, **Type** (full/incr),
**Status** (`success`, `running`, `failed`), **Stream Size**, **Changed**,
**Snapshot** (the snapshot sent), **Date**, and **Restore**.

- **Restore** on a run recreates that dataset from the run's stream. It asks
  twice because it **overwrites the live dataset** with the backup stream.

See [Recover a Dataset](recover-dataset) for the full workflow.

> **Info:** If full/incremental crons are enough, backups run unattended. The
> UI refreshes run status every 5 seconds while a run is in progress, so you
> can watch it complete.

## 5. Capacity planning

A full backup is roughly the **used** size of the dataset (compressed by the
stream). A disk fills up only when a full no longer fits. If a backup disk
reports **Full**, thin out old stream files or use a larger disk for the next
full.

Next: [Handle a Disk Failure](disk-failure)