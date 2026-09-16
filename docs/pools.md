# Set Up a Pool

A ZFS pool is built from disks (or partitions of disks). This page covers the
Disks page (how to wipe and partition drives) and the Pools page (how to turn
them into a pool with the redundancy you want).

## 1. Identify your disks

Open the **Disks** page. The table shows each disk's **Device**, **Model**,
**Serial**, **Size**, **Type**, **Health** and a checkbox per row. **Health**
comes from SMART and is one of **ok**, **failing** or **unknown**. The OS
disks are marked **System** and are protected from accidental use — you will
not be offered them when creating pools.

If a previously-attached disk is missing, it shows **not present**; disks that
have been removed show a **removed** badge. Use **Resurrect** to bring a
**dead** disk back, or **Dead** to mark one as failed (SMART shows **failing**).

> **Info:** A partition or whole disk already used by ZFS, NFS or a declared
> backup disk is marked *used* and disabled wherever a device is picked.

## 2. Wipe and partition disks

NAZMan's "disk group" concept is just **a set of disks receiving the same
layout**. Tick the disks you want to put in the pool, then click
**Partition Selected** (it reads **Partition N Disks** when more than one is
checked).

1. In the **Partition** dialog, add one row per partition using the
   **Add Partition** button.
2. Enter a size per partition, e.g. `10G`, `500M`, `1T` — **leave a row empty
   to use the rest of the disk**.
3. Choose:
   - **Wipe & Partition** — wipes the whole disk and creates the partitions
     (confirm the warning), or
   - **Wipe Only** — wipes all partition tables and leaves the disk blank
     (useful if you just want whole-disk devices).

Every selected disk gets exactly the same layout. Match sizes across rows to
get equal-sized devices (e.g. two `2T` rows per disk for a two-way mirror).

Each partition is given a stable `nazman:<uuid>` GPT partlabel — this is what
lets ZFS vdevs survive Linux renaming `/dev/sdb` → `/dev/sdc`.

> **Tip:** Leave a small partition or two-sized layout spare if you plan to add

> an SLOG later. Partitions on the pool's data disks keep home-type 2–4 disk
> buildst honest without buying more drives.

## 3. Create the pool

Open the **Pools** page and click **Create Pool**.

| Field | What to set |
|---|---|
| **Pool Name** | A short name, e.g. `tank` |
| **Ashift** | Default **12 (4K sectors)** — correct for modern drives; set once, never changeable |
| **Role** | **Data**, **Log (SLOG)**, **Cache (L2ARC)** or **Special** for each vdev |
| **Topology** | **Stripe**, **Mirror**, **RAIDZ1**, **RAIDZ2** or **RAIDZ3** |

Click **Add Device** to pick devices: **Add Selected** from the list. Whole
disks appear as `<device> <size> <type>` tagged **whole disk**; partitions
appear as `<device> p<number>` with a truncated slot-UUID. OS-reserved
partitions are tagged **reserved (OS)** and disabled, and devices already in
use are disabled too.

Choose redundancy to match the value of the data:

| Topology | Needs | Survives |
|---|---|---|
| Stripe | ≥1 disk | 0 failures (fast, risky) |
| Mirror | 2 disks per vdev | 1 failure per vdev |
| RAIDZ1 | 3+ disks | 1 failure per vdev |
| RAIDZ2 | 4+ disks | 2 failures per vdev |
| RAIDZ3 | 5+ disks | 3 failures per vdev |

Add a second vdev of the same shape (click **Add Vdev**) to expand capacity.

Click **Create Pool** and confirm. The pool card shows its **status**, a usage
bar, **Compression x\<ratio\>** and its datasets.

> **Recommended:** start with a **2-way Mirror**, or RAIDZ1 for three disks and
> above on spinning rust.

## 4. Add SLOG / L2ARC / Special vdevs

These are configured at **create time** by adding extra vdevs with roles:

- **Log (SLOG)** — a small, fast, power-safe device (a few GB) to absorb
  synchronous NFS writes. A whole SSD or a small partition works.
- **Cache (L2ARC)** — a large-ish SSD used as a read cache. Loss is harmless
  (it just re-reads from disk).
- **Special** — a device holding metadata and small blocks. Set a dataset's
  **Special Small Blocks** value when you create it to route small files there.

> **Info:** You cannot add SLOG/L2ARC/Special vdevs after the fact from this UI

> — `zpool add` at the shell is the fallback. Plan the layout when you create
> the pool.

## 5. Day-to-day pool operations

On the **Pools** page, the **Pool Operations** card:

- **Scrub Pool** — start a scrub to check and repair data integrity. Run
  monthly on home builds.
- **Export Pool** / **Import Pool** — detach and reattach a pool, e.g. to move
  disks to another machine, or after a reinstall.
- **Metric Logging** — record this pool's disk performance to disk for 30 days
  (see the **Performance** page).
- **Details** on a pool card — vdev-by-vdev breakdown with the state of each
  device (ONLINE / DEGRADED / FAULTED) and the **Last scrub** time.
- **Destroy Pool** — irreversible (see below).

> **Danger:** **Destroy Pool** is destructive and cannot be undone. The

> confirmation shows how much data is stored and warns about **Active NFS
> share!** and **Active user(s)**. You must tick **I understand this is
> irreversible.** to enable the button.

Next: [Create a Dataset](datasets)