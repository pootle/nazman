# Set Up Backups

NAZMan protects you at two levels, both stored on your **external backup
disks**:

1. **Configuration backup** — a bundle of the app's database and host config.
2. **Data backup** — full and incremental streams of your datasets.

Backups live on the **Backup** page; recovery lives on the separate
[Restore](restore) page.

## How data backup is organised

Three nested objects decide where your data goes:

- A **group** is a fixed list of datasets plus two crons. A group always backs
  up *all* of its datasets together.
- A **set** is a ring of backup disks. Sets belong to a group and are used in
  turn: when one fills up, the group moves on to the next, and wraps around at
  the end. Three sets per group gives a rolling three-month rotation.
- A **disk** inside a set is a declared external drive. The set fills disks in
  order, so any one of them can be lost without breaking the group — the
  remaining disks still hold a complete chain.

The first run on a set captures a **full** stream of every dataset in the
group, plus the current configuration. Later runs send only what changed since
the last successful run on that same set.

> **Info:** Because a group backs up all its datasets to one set, two sets can
> compare the same point in time across the whole group — useful for a monthly
> archive, for example.

## 1. Configuration backup

Every backup volume carries its own configuration bundle, so any single disk
can rebuild the whole system. A bundle contains:

- a consistent snapshot of the NAZMan database,
- `/etc/exports` and the NFS server defaults,
- `zpool status`/`zpool get` exports and `sfdisk` partition tables.

Bundles are written **automatically**, never to a local path:

- when a backup disk is declared/formatted, the new volume is seeded with the
  current configuration, and
- at the start of the first backup on a set, so a set used for data always
  carries the matching configuration.

There is no separate "configuration backup" button. To see what a volume
holds, use its **Manifest** action in the **Backup Disks** card. Old bundles
are pruned to a retention count (`backup_config_retention`, default 5) per
volume.

> **Info:** Restoring a configuration bundle overwrites the running
> configuration. It is done from the [Restore](restore) page.

## 2. Declare a backup disk

Data backups go to **external disks** you plug in (USB disks, or a removable
enclosure). The disk is formatted `ext4`; single- or multi-partition disks are
both supported.

Open the **Backup** page and scroll to **Backup Disks**. Click **Declare New
Backup Disk**.

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
   briefly to record its filesystem UUID, and added to the backup disk pool.

A declared disk is not yet used by anything. Add it to a set in step 3.

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
- **Manifest** — view the backup info (pools, datasets, config bundles) stored
  on the volume. Each volume keeps a `nazman-backup.json` index plus a
  self-describing sidecar beside every stream.
- **Rebuild** — regenerate the manifest by scanning the streams and sidecars on
  the volume (useful if the index was lost).
- **Unmount after backup** checkbox — if ticked, NAZMan unmounts the disk after
  each backup/restore so it can be unplugged safely.
- **Remove** — deregisters the disk from NAZMan. Files on the disk are
  preserved; you can re-declare it later.

## 3. Create a backup group

In the **Backup Groups** card click **New Group**.

1. **Name** — e.g. `Media`.
2. **Datasets** — tick every dataset the group should protect. A group backs
   up all of them on every run, so keep it to data that shares a retention
   policy.
3. **Full backup cron** — e.g. `0 2 * * 0` (Sundays at 02:00).
4. **Incremental cron** — e.g. `0 3 * * *` (daily at 03:00). Leave blank to
   run incrementals only when you ask.
5. **Enabled** — untick to keep the group but stop its crons.

Then add sets to it:

- **Add set** — creates a set. **Position** decides the rotation order (0 is
  used first); leave it blank to append at the end. The label is just for
  recognition, e.g. `2026-01`.
- **Add disk** — puts a declared disk at the end of the set's chain. Disks are
  filled in order, so add the spare you want used *next* last.
- **Use** — make a specific disk the next one written to, without waiting for
  the disk to fill.
- **Make active** — start using this set on the next run (use this to rotate
  to a new generation of disks deliberately).

A group is only runnable once it has at least one dataset and **every set has
at least one disk**. Until then the run buttons are disabled and the group says
what is missing.

**Full now** and **Incr now** run the group on demand. **Incr now** still sends
a full stream for any dataset whose anchor is missing, and tells you it did so.

> **Info:** Adding a disk to a set does not move the ones already there. To
> rebuild a set from scratch, delete the set and add a new one — the old
> streams stay on the disks.

## 4. Backup sessions

The **Backup Sessions** card lists every run of a group. A **session** is one
pass over all of the group's datasets:

- **Group** and **Status** — `running…`, `success`, `partial` (some datasets
  failed), or `failed`.
- **Progress** — while a session runs, a bar showing how far through the
  datasets it is, with the current percentage and which dataset it is sending.
- **Datasets** — how many completed, plus any failed or skipped.
- **Written** — bytes streamed.
- **Set** — which set and which disk of it the session wrote to.
- **Started** — when it began.

The page refreshes every 5 seconds while a session is running, so you can
watch it complete. To see a session's per-dataset streams, use the **Manifest**
action on the disk it wrote to.

### Monitoring a running backup

The dataset count and the **Written** figure are exact: they update every five
seconds, a step for each dataset finished. The percentage inside the current
dataset is an **estimate**, not a byte-accurate figure:

- NAZMan compares the bytes written so far against the size of the last
  successful stream of that dataset, so a compressible dataset that changes a
  lot between runs can sit at a percentage and race ahead, or arrive late.
- The estimate is capped at 99% until the stream finishes, so the final
  checksum step never shows 100%.
- The very first backup of a dataset has nothing to compare against: it shows
  the bytes written and "estimating…" rather than a made-up percentage.

## 5. Capacity and rotation

A full backup is roughly the **used** size of the datasets in the group
(compressed by the stream); an incremental is only the changes since the last
run on that set. A set's **Free** figure sums the disks still available in it.

When a disk fills up mid-run, NAZMan keeps the snapshot and the session it has
already written, moves to the next disk in the same set, and continues there.
The session reports `partial` so you know it spanned two disks.

When every disk in a set is full, the group's next run moves to the next set
in the ring. If the group is marked **needs a disk**, all of its sets are full
or an offline disk is blocking it — plug in a disk, or add a set with room.

You can force the move early with **Disk full — next** on a set, which is the
same as clicking **Use** on the following disk.

### Copies (redundancy)

A group's **Copies** setting (default 1 = the normal rotating ring) writes each
backup to that many consecutive sets instead of one. With two sets it keeps two
full copies; with the setting equal to the number of sets, every disk receives
every backup. Each targeted set keeps its own independent chain (the snapshot is
sent once per disk), so it costs that many times as much CPU and disk to stream.

### Recycling full disks

With **Recycle full disks** enabled, a full disk's turn does not block the group:
NAZMan wipes and reformats the disk, then restarts its chain with a fresh full
backup. This frees you from rotating the media, but it **permanently erases
everything stored on that disk** — the current backup and the entire older
chain. Leave that disk unplugged if you still need it as a restore source.
Disable the setting and the group blocks with **needs a disk** instead, as
described above.

The **Restore** page's backup-sets menu marks the disk(s) holding the newest
backup of each group with a **latest** badge.

Next: [Restore & Rebuild](restore)
