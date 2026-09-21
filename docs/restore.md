# Restore & Rebuild

The **Restore** page recovers data and configuration from your backup disks.
It has three parts: configuration restore, single-dataset restore, and a
step-by-step **Rebuild This System** wizard for new hardware or a fresh
install.

## 1. Restore configuration

The **Restore Configuration** card lists every configuration bundle found
across your backup volumes. Pick the bundle you want and click **Restore**
(twice) — this overwrites the NAZMan database and host configuration. See
[Set Up Backups](backup) for what a bundle contains.

## 2. Restore a dataset

- **From a backup run** — the table lists successful runs; **Restore** asks for
  a target dataset name and overwrites it from that stream.
- **From a stream file** — pick a mounted backup disk, a stream file, and a
  target dataset name, then **Restore from file**. This works even on a fresh
  server with no run history.

## 3. Rebuild This System

Use this after replacing hardware or reinstalling the OS. It works entirely
from the backup disks — the running database is not required.

### Step 1 — Find backup sets

Connect your backup disks and click **Scan disks**. NAZMan mounts each
candidate read-only, reads its manifest, and lists the backup info sets it
finds (volume, device, pools, datasets, config bundles). Select one.

### Step 2 — Review the set

The set detail shows the recorded pools and their vdev topologies, the dataset
list, the configuration bundles it carries, and which backup media are
required.

### Step 3 — Recreate pools

For each pool, every recorded vdev slot is shown with the original device
identity (by-id / serial / size). Assign an attached disk to each slot — matched
disks are pre-selected by by-id, then serial.

- **Prepare partitions** — for pools on partitions, recreate the recorded GPT
  layout (including each `nazman:<uuid>` slot label) on the assigned disks.
  This **wipes** those disks.
- **Create pool** — create the pool from the assigned disks.
- **Skip** — leave this pool for later.

> **Danger:** Creating a pool destroys data on the selected devices. Assign
> disks carefully; the recorded identities are shown to help.

### Step 4 — Restore datasets

Each dataset from the set is listed with a checkbox (on by default) and a
**Target pool** dropdown, pre-selected when a pool of the same name exists.
Adjust the target pool if you renamed pools, then **Restore selected**.

If datasets live on more than one backup disk, the **Required backup media**
list shows which disks are connected. Insert each disk and use **Restore from
this disk** to restore the datasets stored on it.

### Step 5 — Finish (optional)

- **Adopt backup media** — re-register the backup volume as a declared backup
  disk so future backups work.
- **Rebuild schedules** — reconcile the restored backup schedules into running
  scheduler jobs.

> **Summary:** Scan → review → recreate pools → restore datasets (inserting each
> disk as prompted) → adopt media and rebuild schedules. Configuration can be
> restored from any volume's bundle at any point.

Next: [Set Up Telegram Alerts](alerts)
