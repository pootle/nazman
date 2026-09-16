# Share Data (NFS & SMB)

Datasets become useful to other machines when shared. NAZMan supports **NFS v4**
and **SMB (Samba)**. You need a dataset first — see
[Create a Dataset](datasets).

Both share types map every client to the shared anonymous user
(`nfsanon`, UID/GID 65533), so NFS and SMB clients see consistent ownership.

## NFS

Open the **NFS** page.

> **Info:** If the kernel NFS server is not installed, a warning card appears:
> *"The NFS kernel server is not installed on this server. NFS sharing will not
> work until you install it."* Click **Install NFS server** and wait for the
> toast. The Create/Refresh buttons stay disabled until it is installed.

### Create an export

Click **Create Share** and fill in the **Create NFS Share** dialog:

| Field | What to set |
|---|---|
| **Dataset** | The dataset to export, e.g. `tank/media` |
| **Client Specification** | One host/CIDR, e.g. `192.168.1.0/24`, or `*` for all hosts |
| **Read/Write** | Always on (checked, disabled) |
| **Sync** | Left checked; async risks lost writes on power loss |
| **No Subtree Check** | Left checked for cleaner semantics |
| **No Hide (NFSv4)** | Check to export datasets that have children |

Click **Save Share** and confirm. The share appears in the **NFS Shares** table
with status **Shared**, and in **Active Exports** showing the **Path**, **Client**
and **Options**.

### Manage exports

- Pause/play icon on a row — **Paused** stops the export without removing it.
- Pencil icon — edit the client specification or options.
- Trash icon — remove the export permanently.

> **Info:** Every client is squashed to the shared anonymous user for
> consistent read/write access. Fine-grained per-user permissions are outside
> NAZMan's scope.

## SMB

Open the **SMB** page. As with NFS, a warning card offers **Install Samba**
first if it is missing.

### Create a share

Click **Create Share** and fill in the **Create SMB Share** dialog:

| Field | What to set |
|---|---|
| **Dataset** | The dataset to share |
| **Read-only share** | Tick to export read-only; unticked = read-write |

Click **Save Share** and confirm. The share appears with its **Path**,
**Share Name**, **Access** (**Read-only** or **Read-write**) and status
**Shared**.

> **Info:** All SMB shares allow guest (anonymous) access, with reads/writes
> forced to `nfsanon` to stay consistent with NFS.

### Manage shares

- Pause/play icon — **Enabled** / **Disabled**.
- Pencil icon (only while enabled) — edit the share.
- Trash icon — remove the share.

## Next

With shares working, protect the data — [Snapshots](snapshots) come next, then
[Set Up Backups](backup).