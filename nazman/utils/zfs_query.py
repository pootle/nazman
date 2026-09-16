"""Shared live-ZFS query helpers.

Several managers need the same handful of read-only ZFS facts (does a dataset
exist, what filesystems live under a root, which NFS clients are connected).
Centralising them here keeps the ZFS CLI surface in one auditable place and
removes the duplicated parse loops that previously drifted apart.
"""

from typing import Dict, List, Optional

from .commands import run_command, run_zfs, run_zpool
from .devices import normalize_base_name


async def dataset_exists(dataset_name: str) -> bool:
    """Confirm a dataset currently exists in ZFS by its full name."""
    stdout, _, rc = await run_zfs(
        "list", "-H", "-o", "name", dataset_name, check=False, op="read"
    )
    return rc == 0 and dataset_name in stdout.split()


async def all_filesystem_names() -> List[str]:
    """Full names of every filesystem dataset on the host, excluding pool roots."""
    stdout, _, rc = await run_zpool("list", "-H", "-o", "name", check=False, op="read")
    if rc != 0:
        return []
    roots = [line.strip() for line in stdout.splitlines() if line.strip()]
    names: List[str] = []
    for root in roots:
        names.extend(await list_filesystem_names(root))
    return names


async def list_filesystem_names(root: str) -> List[str]:
    """Full names of every filesystem dataset under ``root``, excluding root."""
    stdout, _, rc = await run_zfs(
        "list", "-H", "-o", "name", "-t", "filesystem", "-r", root,
        check=False, op="read",
    )
    if rc != 0:
        return []
    return [line.strip() for line in stdout.splitlines()
            if line.strip() and line.strip() != root]


async def showmount_clients(paths: List[str]) -> List[Dict[str, str]]:
    """NFS clients currently connected to any of the given export paths.

    Parses ``showmount -a`` ("client:host/path" lines) and returns
    ``{"client", "path"}`` dicts for entries whose path equals or sits under
    one of ``paths``.  Returns an empty list when showmount is unavailable or
    the NFS server is not running.
    """
    clients: List[Dict[str, str]] = []
    try:
        stdout, _, rc = await run_command(["showmount", "-a"], timeout=15, check=False)
        if rc != 0:
            return clients
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("All mount") or ":" not in line:
                continue
            client, path = line.rsplit(":", 1)
            path = path.strip()
            if any(path == p or path.startswith(p + "/") for p in paths):
                clients.append({"client": client.strip(), "path": path})
    except Exception:
        pass  # showmount unavailable or no NFS server
    return clients


def pool_member_for_by_id(pool_members: Dict[str, str], by_id: Optional[str]) -> Optional[str]:
    """Return the pool owning ``by_id`` (exact, basename, or any -partN child).

    ``pool_members`` is a map from device path -> pool name as produced by
    ``zpool status -j`` parsing.  Partitioned pool members are matched via
    their ``-partN`` children.
    """
    if not by_id:
        return None
    basename = by_id.rsplit("/", 1)[-1]
    for key in (by_id, basename):
        if key in pool_members:
            return pool_members[key]
    for dev, pool in pool_members.items():
        if dev.startswith((f"{by_id}-part", f"{basename}-part")):
            return pool
    return None


def pool_member_for_disk(pool_members: Dict[str, str], disk) -> Optional[str]:
    """Return the name of the pool that owns ``disk`` (whole disk or any partition)."""
    return pool_member_for_by_id(pool_members, disk.by_id if disk else None)


def pool_vdev_bases(
    status_info: Dict,
    valid_bases: List[str],
    groups: tuple = ("data_vdevs", "special_vdevs", "log_vdevs", "cache_vdevs"),
    limit: Optional[int] = None,
) -> List[str]:
    """Base disk names of a pool's vdev leaves, restricted to ``valid_bases``.

    ``status_info`` is the parsed dict from ``zpool status -j`` (as returned
    by ZfsManager.get_pool_status).  Groups are scanned in order so data
    disks take precedence over special/log/cache on the dashboard.
    """
    bases: List[str] = []
    for group in groups:
        for vdev in status_info.get(group, []):
            for child in vdev.get("children", []):
                leaf = child.get("name") or child.get("path") or ""
                base = normalize_base_name(leaf)
                if base in valid_bases and base not in bases:
                    bases.append(base)
                if limit is not None and len(bases) >= limit:
                    return bases[:limit]
    return bases[:limit] if limit else bases
