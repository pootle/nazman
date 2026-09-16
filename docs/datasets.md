# Create a Dataset

Datasets are the filesystems you actually store and share. Everything lives
**inside a pool**, so set the pool up first — see [Set Up a Pool](pools).

## 1. Create the dataset

Open the **Datasets** page and click **Create Dataset**.

The **Create ZFS Dataset** dialog:

| Field | Recommendation |
|---|---|
| **Pool** | The pool from the previous step |
| **Dataset Name** | e.g. `media`, or `data/media` for a nested path |
| **Compression** | **zstd (recommended)** for media, **lz4** for archive-style data, or **Off** |
| **Atime** | **Partial (recommended)** — note access times only when something changes |
| **Sync** | **Standard**, or **Always (safer)** if you are paranoid about power loss |
| **Quota** | Optional cap, e.g. `500G`, `1T` (leave blank for unlimited) |
| **Record Size** | **128K (default)** for general/media; **1M (media)** for large sequential files; **4K (databases)** for DBs |
| **CanMount** | **On** (default); **Off** or **NoAuto** if something else mounts it |
| **Read Only** | **Off** unless you want a frozen read-only tree |
| **Special Small Blocks** | **Off (disabled)**, or a size (4K–128K) to send small files/blocks to a special vdev |

Click **Create Dataset** and confirm. The dataset appears in the
**ZFS Datasets** table with its compression, atime, sync, record size, mount
options and usage.

> **Tip:** Recommended settings (zstd, atime Partial, 128K records) are already
> pre-filled. Only change what you have a reason to change.

## 2. Edit a dataset

Use the pencil icon on the row. The **Edit ZFS Dataset** dialog has the same
fields (Dataset Name is fixed). Enter a quota here, or leave **blank to clear**
an existing quota. Click **Save Changes**.

## 3. Properties you can rely on

- **Compression** is transparent — clients read and write plain files, so you
  can enable zstd on a dataset that already has data.
- **Record Size** affects how much RAM ZFS uses per block and how well media
  files compress; changing it only affects newly written data.
- **Quota** is a soft measure for users, not a safety mechanism — a quota limits
  dataset usage, it does not protect you from deletion or disk failure.

## 4. Destroying a dataset

The trash icon on the row is irreversible:

- NAZMan first checks the dataset **must be unmounted** and **has no active NFS
  clients** (`Cannot destroy ... Unmount the dataset and disconnect NFS clients
  first.`).
- You are asked whether to destroy **ALL child datasets and snapshots**
  (recursive) or only if no children exist.
- The final confirmation warns this **cannot be undone**.

> **Danger:** There is no refuse bin. If you might need the data later, take a

> [snapshot](snapshots) or a [backup](backup) first.

## 5. Where datasets next appear

Once a dataset exists you can:

- **Share it** over NFS and/or SMB — see [Share Data](shares).
- **Snapshot it** — see [Snapshots](snapshots).
- **Back it up** to an external disk — see [Set Up Backups](backup).

Next: [Share Data (NFS & SMB)](shares)