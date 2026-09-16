# Handle a Disk Failure

How disks fail, what you see in NAZMan, and how to respond. The right recovery
depends on the pool's redundancy — a mirror or RAIDZ vdev keeps working while a
failing disk is replaced; a stripe takes the whole pool down with one disk.

## What you will see

- **Dashboard** — an unhealthy pool card is highlighted for any
  `health != ONLINE` pool.
- **Pools** → **Details** — the vdev breakdown shows the device **state**
  (ONLINE / DEGRADED / FAULTED / OFFLINE).
- **Disks** — SMART **Health** shows **failing**; a physically missing disk
  shows **not present**, then **removed**.
- **Command Log** — errors from `zpool status`, `smartctl`, etc. are recorded
  here with `failed` status and the stderr detail.

> **Info:** If only the SMART *health* is worrying but the device still

> responds, back the data up now (see [Set Up Backups](backup)) *before* the
> disk goes read-only.

## Step 1 — Identify the failing disk

Open the **Pools** page, click **Details** on the pool, and read the device
state. The **Disks** page tells you which physical device that maps to by
**Model**/**Serial**. NAZMan tracks disks by stable `by-id` paths and slot
UUIDs, so the *name* is stable even though Linux renumbers the kernel device.

## Step 2 — Mark the disk dead

On the **Disks** page, mark the failed drive **Dead** (confirmation prompts).
For a disk that is already gone, the row shows **removed**; use **Drop** to
remove a permanently-gone disk from the database.

This is bookkeeping — NAZMan does not remove the device from ZFS itself.

## Step 3 — Replace the device

ZFS replacement is done at the command line (NAZMan deliberately leaves the
array surgery to `zpool`). Log in as root (`sudo -i`):

```bash
# 1. Physically remove the failed disk and fit the replacement.
#    Confirm which device name zpool reports for the failed vdev:

zpool status tank

# 2. In a mirror or RAIDZ vdev, you can start the replacement online:

zpool replace tank <old-device> <new-by-id-device>
```

Use the stable by-id name for the new device, e.g.:

```bash
zpool replace tank ata-WDC-WD40EFAX-XXXXXXXX-part1 \
                 ata-WDC-WD40EFAX-YYYYYYYY-part1
```

ZFS immediately begins a **resilver** — it recalculates the replacement from
the surviving devices. Watch progress:

```bash
zpool status tank        # Resilvered: N% - shows ETA
```

> **Tip:** Give the new disk the *same* partition layout as its siblings
> (`sgdisk`/`parted` at the shell, or NAZMan's **Partition Selected** on the
> Disks page before replacing) so the vdev stays clean.

## Pool states while you work

| `zpool status` state | Meaning | NAZMan shows |
|---|---|---|
| ONLINE | healthy | green status badge |
| DEGRADED | a device is missing/failed but data is complete (mirror/RAIDZ) | **DEGRADED** badge |
| FAULTED | a device is unusable and reads hang until it is replaced | **FAULTED** badge |
| UNAVAIL | pool vdevs offline | **UNAVAIL** badge |

> **Danger:** A **Stripe** pool has no redundancy. One failed disk means the

> pool is **UNAVAIL** and requires restoring backups to a new pool — there is
> no in-place repair. See [Recover a Complete System](recover-system).

## After replacement

1. Let the resilver finish (a multi-TB mirror can take hours simultaneously
   with normal use).
2. The pool returns to **ONLINE**; the failing drive should now be **removed**
   on the Disks page — **Drop** it to clean up.
3. Check **Backup** → **Backup Runs** for a recent success, and re-run a backup
   if the last one was before the failure.

Next: [Recover a Dataset](recover-dataset)