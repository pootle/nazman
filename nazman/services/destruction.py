"""Pool/dataset destruction orchestration across ZFS, NFS and SMB domains.

Destroying storage is the one workflow that legitimately spans sharing
managers: a pool cannot be destroyed while its datasets are mounted, held by
NFS clients, or shared over SMB, and its exports/shares must be torn down
first. Composing that here keeps ZfsManager, NfsManager and SmbManager free of
one another.
"""

import re
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..utils.commands import run_command, run_zfs, run_zpool
from ..utils.exceptions import DatasetError, PoolError
from ..utils.validation import validate_pool_name
from ..utils import zfs_query


class DestructionService:
    """Checks destroy obstacles and tears down pools/datasets with their shares."""

    def __init__(self, zfs, nfs, smb) -> None:
        self.zfs = zfs
        self.nfs = nfs
        self.smb = smb

    # ── Unmounting ──────────────────────────────────────────────────────

    async def _mounted_names(self, names: List[str]) -> List[str]:
        """Return the subset of ``names`` currently mounted by ZFS."""
        mounted = []
        for ds in names:
            try:
                stdout, _, rc = await run_zfs(
                    "get", "-H", "-o", "value", "mounted", ds, check=False, op="read",
                )
            except Exception:
                continue
            if rc == 0 and stdout.strip().lower() == "yes":
                mounted.append(ds)
        return mounted

    async def _unmount_names(self, names: List[str]) -> None:
        """Unmount every mounted dataset in ``names``, raising on any that is busy.

        When ``zfs unmount`` is refused the most specific cause is surfaced: an
        open SMB network drive or connected NFS client pinning the dataset, else
        a local process holding the mountpoint.
        """
        for ds in await self._mounted_names(names):
            _, stderr, rc = await run_zfs(
                "unmount", "-f", ds, check=False, op="write",
            )
            if rc != 0:
                raise DatasetError(await self._busy_hint(ds, stderr))

    async def _smb_clients_for(self, dataset_name: str) -> List[str]:
        """Hosts with a live SMB session on ``dataset_name``'s share, if any.

        Parses the ``Service`` table of ``smbstatus`` (share -> pid -> machine),
        matching the trailing dataset name against the share. Returns an empty
        list when smbstatus is unavailable or no session is open.
        """
        share = dataset_name.rsplit("/", 1)[-1]
        try:
            stdout, _, rc = await run_command(
                ["smbstatus"], timeout=15, check=False, op="read", category="smb",
            )
        except Exception:
            return []
        if rc != 0:
            return []
        hosts: List[str] = []
        in_services = False
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("Service"):
                in_services = True
                continue
            if not in_services or not line:
                continue
            cols = line.split()
            if len(cols) >= 3 and cols[0] == share:
                host = cols[2]
                if host not in hosts:
                    hosts.append(host)
        return hosts

    async def _busy_hint(self, dataset_name: str, stderr: str) -> str:
        """Explain why a dataset could not be unmounted, as specifically as possible."""
        reason = stderr.strip() or "dataset is busy"
        smb_hosts = await self._smb_clients_for(dataset_name)
        if smb_hosts:
            hosts = ", ".join(smb_hosts)
            return (
                f'Could not unmount "{dataset_name}": an open SMB connection from '
                f"{hosts} is keeping it in use ({reason}). "
                f"Close the mapped network drive on {hosts} and retry."
            )
        nfs_clients = await zfs_query.showmount_clients([f"/{dataset_name}"])
        if nfs_clients:
            hosts = ", ".join(sorted({c["client"] for c in nfs_clients}))
            return (
                f'Could not unmount "{dataset_name}": NFS client(s) {hosts} are still '
                f"connected to it ({reason}). Unmount the share on those clients and retry."
            )
        return (
            f'Could not unmount "{dataset_name}": {reason}. '
            "A local process may still be using its mountpoint."
        )

    # ── Pools ────────────────────────────────────────────────────────────

    async def get_pool_destroy_info(self, db: Session, pool_name: str) -> Dict[str, Any]:
        """Gather info shown in the pool-destroy confirmation dialog."""
        validate_pool_name(pool_name)

        size_bytes = used_bytes = free_bytes = None
        try:
            stdout, stderr, rc = await run_zpool(
                "list", "-p", "-H", "-o", "name,size,allocated,free", pool_name,
                check=False, op="read",
            )
            if rc == 0 and stdout.strip():
                parts = stdout.split()
                if len(parts) >= 4:
                    try:
                        size_bytes = int(parts[1])
                        used_bytes = int(parts[2])
                        free_bytes = int(parts[3])
                    except ValueError:
                        pass
        except Exception:
            pass

        pool = self.zfs.get_pool_by_name(db, pool_name)
        export_info = {"exports": [], "active_clients": []}
        if pool is not None:
            export_info = await self.nfs.get_pool_export_info(db, pool)
        smb_info = {} if pool is None else self.smb.get_pool_share_info(db, pool)

        return {
            "pool_name": pool_name,
            "size_bytes": size_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "has_active_export": bool(export_info["exports"]),
            **export_info,
            "smb": smb_info,
        }

    async def _pool_destroy_obstacles(self, db: Session, pool_name: str) -> Dict[str, Any]:
        """Return obstacles that would prevent destroying a busy pool.

        Mirrors the dataset-destroy pre-check: a mounted dataset kept in use by
        an active NFS client (or a local process) causes `zpool destroy -f` to
        fail with "cannot unmount '<mountpoint>'". Collect each mounted child
        dataset's mountpoint and any connected NFS clients so the frontend can
        guide the user to unmount and disconnect before retrying.
        """
        mounted = []
        dataset_names = await zfs_query.list_filesystem_names(pool_name)

        for ds_name in dataset_names:
            mount_path = f"/{ds_name}"
            try:
                stdout, _, rc = await run_zfs(
                    "get", "-H", "-o", "value", "mounted", ds_name, check=False, op="read",
                )
                if rc == 0 and stdout.strip().lower() == "yes":
                    mounted.append(mount_path)
            except Exception:
                pass

        # Active NFS clients across every dataset mount path in the pool.
        probe_paths = [f"/{name}" for name in dataset_names] or [f"/{pool_name}"]
        active_clients = await zfs_query.showmount_clients(probe_paths)

        # Active SMB connections on any dataset share in the pool.
        try:
            smb_connected = await self._smb_active_clients(probe_paths)
        except Exception:
            smb_connected = []

        return {
            "mounted": mounted,
            "active_clients": active_clients,
            "smb_connected": smb_connected,
        }

    async def _smb_active_clients(self, probe_paths: List[str]) -> List[str]:
        """Datasets with an active SMB connection (via smbstatus), if available."""
        connected = []
        try:
            stdout, _, rc = await run_command(
                ["smbstatus", "-b"], timeout=15, check=False,
                op="read", category="smb",
            )
            if rc == 0:
                # smbstatus -b shows service + pids; match on the service/path
                # name (the trailing share name equals the dataset basename).
                for line in stdout.splitlines():
                    line = line.strip()
                    m = re.search(r"\b(\S+)\]?\s+\d+", line)
                    if not m:
                        continue
                    svc = m.group(1).strip("[]")
                    linked = [p for p in probe_paths if p.rsplit("/", 1)[-1] == svc]
                    if linked:
                        connected.append(linked[0])
        except Exception:
            pass
        return connected

    async def destroy_pool(self, db: Session, pool_name: str) -> None:
        """Destroy a pool (DESTRUCTIVE). Removes DB record."""
        validate_pool_name(pool_name)

        pool = self.zfs.get_pool_by_name(db, pool_name)

        # Pre-check: block if any child dataset is held by an NFS/SMB client.
        # Otherwise mounted datasets are unmounted cleanly below; `zpool destroy
        # -f` force-unmounts, but the explicit unmount surfaces a busy
        # mountpoint (local process) as a dataset-specific error first.
        obstacles = await self._pool_destroy_obstacles(db, pool_name)
        if obstacles["active_clients"] or obstacles.get("smb_connected"):
            parts = []
            if obstacles["active_clients"]:
                n = len(obstacles["active_clients"])
                parts.append(f"{n} NFS client(s) still connected")
            if obstacles.get("smb_connected"):
                n = len(obstacles["smb_connected"])
                parts.append(f"{n} SMB connection(s) still active on {', '.join(obstacles['smb_connected'])}")
            raise PoolError(
                f'Pool "{pool_name}" has {"; ".join(parts)}. '
                "Disconnect NFS/SMB clients before destroying the pool."
            )

        # Unexport any NFS shares and unshare any SMB shares owned by this pool
        if pool is not None:
            try:
                await self.nfs.unexport_pool(db, pool)
            except Exception:
                pass
            try:
                await self.smb.unshare_pool(db, pool)
            except Exception:
                pass

        dataset_names = await zfs_query.list_filesystem_names(pool_name)
        await self._unmount_names([pool_name] + dataset_names)

        stdout, stderr, returncode = await run_zpool(
            "destroy", "-f", pool_name,
            timeout=300
        )

        if returncode != 0:
            raise PoolError(f"Failed to destroy pool: {stderr}")

        if pool:
            db.delete(pool)
            db.commit()

    # ── Datasets ─────────────────────────────────────────────────────────

    async def _dataset_destroy_obstacles(self, db: Session, dataset_name: str) -> Dict[str, Any]:
        """Return obstacles that would prevent destroying a mounted/busy dataset.

        ``mounted`` reflects whether the dataset filesystem is currently mounted,
        ``exports`` lists any defined NFS exports (informational), and
        ``active_clients`` lists NFS clients currently connected to the dataset's
        mount path. Active clients keep the mount busy even after the share is
        removed, which is the common cause of a "cannot unmount" destroy failure.
        """
        mounted = False
        try:
            stdout, _, rc = await run_zfs(
                "get", "-H", "-o", "value", "mounted", dataset_name, check=False, op="read",
            )
            if rc == 0 and stdout.strip().lower() == "yes":
                mounted = True
        except Exception:
            pass

        exports = []
        sharenfs = await self.nfs.read_sharenfs(dataset_name)
        if sharenfs not in ("off", ""):
            exports = [f"/{dataset_name}"]

        active_clients = await zfs_query.showmount_clients([f"/{dataset_name}"])

        try:
            smb_connected = await self._smb_clients_for(dataset_name)
        except Exception:
            smb_connected = []

        return {
            "mounted": mounted,
            "exports": exports,
            "active_clients": active_clients,
            "smb_share": self.smb.list_shares(db),
            "smb_connected": smb_connected,
        }

    async def destroy_dataset(self, db: Session, dataset_name: str, recursive: bool = False) -> None:
        """Destroy a dataset (DESTRUCTIVE).

        Mounted datasets are unmounted cleanly first (recursively when
        ``recursive``); an active NFS client, a live SMB share, or an open SMB
        connection hard-blocks with a specific message.
        """
        obstacles = await self._dataset_destroy_obstacles(db, dataset_name)
        smb_share = next((s for s in (obstacles.get("smb_share") or [])
                          if s["dataset_name"] == dataset_name), None)
        smb_connected = obstacles.get("smb_connected") or []
        if obstacles["active_clients"] or smb_share or smb_connected:
            parts = []
            if smb_connected:
                hosts = ", ".join(smb_connected)
                parts.append(f"held open by an SMB connection from {hosts}")
            elif smb_share:
                parts.append("shared over SMB")
            if obstacles["active_clients"]:
                n = len(obstacles["active_clients"])
                parts.append(f"connected to by {n} NFS client(s)")
            message = f'Dataset "{dataset_name}" is {" and ".join(parts)}.'
            if smb_connected:
                message += (
                    f" Close the mapped network drive on {', '.join(smb_connected)} "
                    "and retry."
                )
            elif smb_share:
                message += " Remove the SMB share first."
            else:
                message += " Unmount those clients before destroying."
            raise DatasetError(message)

        names = [dataset_name]
        if recursive:
            names += await zfs_query.list_filesystem_names(dataset_name)
        await self._unmount_names(names)

        cmd = ["destroy"]
        if recursive:
            cmd.append("-r")
        cmd.append(dataset_name)

        stdout, stderr, returncode = await run_zfs(*cmd, timeout=300, check=False)

        if returncode != 0:
            raise DatasetError(f"Failed to destroy dataset: {stderr}")
