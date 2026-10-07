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
candidate read-only, reads its manifest, and lists the backup sets it finds
(pools, datasets, config bundles, and how many disks each set spans). Select
one.

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

A backup set is a **chain**: its disks hold successive parts of the same
datasets, and each disk's manifest names the set it belongs to. NAZMan groups
the disks it finds by that stamp, so a set spread over several disks is listed
once with a **Disks** count instead of appearing several times.

The **Required backup media** list shows every disk that holds part of the
chain and whether it is connected. Insert all of them — a chain cannot be read
without each disk that holds part of it. A dataset whose chain continues onto
a disk that is missing is marked, and restoring it fails rather than silently
producing a partial dataset.

Media written before backup sets existed has no set stamp; each such volume is
offered as its own single-disk set, exactly as before.

### Step 5 — Finish (optional)

- **Adopt backup media** — re-register the restored backup disks as declared
  backup disks so future backups can use them. Every disk of the set is
  adopted; any that cannot be (no filesystem UUID, or not matched to an
  attached disk) is listed so you can deal with it.
- **Rebuild schedules** — reconcile the restored backup groups' crons into
  running scheduler jobs.

> **Summary:** Scan → review → recreate pools → restore datasets (inserting each
> disk as prompted) → adopt media and rebuild schedules. Configuration can be
> restored from any volume's bundle at any point.

Next: [Set Up Telegram Alerts](alerts)
