# Recover a Dataset

You deleted data, rolled forward past a mistake, or corrupted a dataset. Your
options, in order of speed:

1. **Backup run restore** — restore a dataset that was backed up, from its
   stored stream file.
2. **Restore from backup disk** — pull a dataset from a stream file already on
   a mounted backup disk, even on a fresh server.

> **Info:** Both options work on the **Backup** page and both **overwrite the

> target dataset** with the backup stream. If the dataset still has data you
> want, snapshot it first (`zfs snapshot` or NAZMan's Snapshot page).

## Before you start

- The backup disk must be **plugged in** and show **Mounted** on the
  **Backup Disks** card. If it reads **Not connected**, try **Wake / Replug**,
  then power-cycle the enclosure if necessary.
- The disk must have been declared with the correct filesystem **UUID
  unchanged** — if the status reads **Filesystem changed**, the wrong disk
  (or a re-formatted one) is plugged in. Re-scan or double-check the physical
  drive.

## Option A — Restore from a specific backup run

The **Backup Runs** card lists recent runs for every dataset.

1. Find the run you want (check **Dataset**, **Type**, **Date** and
   **Snapshot**).
2. Click **Restore** in that row.
3. Confirm the first prompt — *"Restore dataset "<name>" from this backup
   run?"* — then the second, harder one — *"Are you REALLY sure? Live data will
   be replaced."*

NAZMan mounts the disk if needed, streams the run's snapshot into the dataset,
and reports **Restored: <dataset> from <source>**.

> **Note:** The **Restore** button is disabled while a run is in progress
> (em-dash shown).

## Option B — Restore from any stream file on a disk

Use the **Restore from Backup Disk** card when the dataset runs were wiped or
abandoned machine, or for a fresh server. It scans a **mounted** backup disk for
stream files regardless of the stored run records.

1. **Select backup disk...** — pick the mounted disk.
2. **Select stream file...** — pick the pair shown as
   `<dataset> | <filename> (<size>)`.
3. **Dataset name to restore, e.g. tank/data** — where to put it. On a fresh
   server this shows the pool must already exist (see
   [Recover a Complete System](recover-system)).
4. Click **Restore** and confirm — *"Restore dataset "<name>" from <filename>?
   This will overwrite any existing dataset."*

The stream is decompressed and received with `zfs receive`, so the restored
dataset comes up exactly as it was at backup time, including compression.

## Important notes

- A **full** restore replaces the whole dataset. There is no partial/merge.
  Apply your writes again on top afterwards.
- Chain restore to a *different* name if you only want to *copy* data out:
  restore to a scratch dataset (e.g. `tank/recovered`), then `zfs send | zfs
  receive` the bits you need, or copy files via the dataset's mountpoint.
- Keep the same pool on a fresh server, otherwise the stream refuses to match
  upstream properties — restore onto a same-shaped pool and dataset always wins
  with **-F**.

Next: [Recover a Complete System](recover-system)