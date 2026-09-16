# Snapshots

A ZFS snapshot is a read-only, instant copy of a dataset at a point in time.
It costs almost nothing until data changes, and it is the foundation of the
backup system (backup streams are taken from snapshots).

## Take a snapshot

Open the **Snapshots** page and click **Create Snapshot**.

1. **Dataset:** pick the dataset.
2. **Snapshot Name:** e.g. `backup-2024-01-15` (a `<dataset>@<name>` snapshot).
3. Click **Create Snapshot** and confirm.

The **Snapshots** table lists every snapshot with its **Dataset**,
**Snapshot**, **Used** and **Referenced** sizes. Filter by dataset using the
dropdown (defaults to **All Datasets**) and **Refresh**.

> **Tip:** The whole dataset can be rolled back to any snapshot later by
> restoring its stream; the Backup system names its own snapshots with a
> `backup-` prefix so you can tell them apart.

## Destroy a snapshot

- Use the trash icon on a row, or the **Snapshot Operations** card (**Destroy
  Snapshot**) — both ask for confirmation, and destruction **cannot be undone**.
- **Snapshot Info** in the same card opens details for a selected snapshot.

## Recurring snapshots

The UI's per-dataset **Full/Incr cron** fields on the [Backup](backup) page
create snapshots automatically as part of each backup run. There is no
separate "scheduled snapshots" screen — schedule recurring point-in-time
snapshots there, or via `cron` at the shell.

> **Info:** Snapshots are kept on the same disks as the data they protect. They
> guard against accidental deletion and corruption, **not** against drive
> failure. A disk that dies takes its snapshots with it. For protection against
> disk loss you need [backups](backup) on a separate disk.

Next: [Set Up Backups](backup)