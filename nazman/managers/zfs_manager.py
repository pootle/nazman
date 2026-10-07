from typing import List, Optional, Dict, Any, Tuple
import json
import re
from sqlalchemy.orm import Session

from ..models.pool import Pool
from ..models.disk import Disk
from ..utils.commands import run_zpool, run_zfs
from ..utils.devices import (
    read_slot_uuids, resolve_slot_to_device, get_device_path, get_device_name,
    partition_by_id, kernel_base_name, normalize_base_name,
)
from ..utils.exceptions import (
    PoolError, PoolNotFoundError, DatasetError, DatasetNotFoundError, ValidationError,
)
from ..utils.sizes import parse_size_to_bytes
from ..utils.validation import (
    validate_pool_name, validate_dataset_name,
)
from ..utils import zfs_query


def _parse_size_bytes(size_str: str) -> float:
    """Parse a ZFS size string (e.g. '3.64T', '1024M', or raw bytes)."""
    return float(parse_size_to_bytes(size_str))


def _vdev_usable_bytes(vdev: Dict[str, Any]) -> float:
    """Usable bytes of a single data vdev after redundancy overhead.

    Replicates the frontend's vdevSize() math: mirrors count the smallest
    device, RAIDZ1/2/3 subtract 1/2/3 parity devices, stripes sum all.
    """
    children = vdev.get("children") or []
    sizes = [_parse_size_bytes(c.get("size")) for c in children]
    sizes = [s for s in sizes if s > 0]
    if not sizes:
        return 0.0
    vtype = vdev.get("type") or "stripe"
    if vtype == "mirror":
        return min(sizes)
    if vtype == "raidz1":
        return sum(sizes) - min(sizes)
    if vtype == "raidz2":
        return sum(sizes) - sum(sorted(sizes)[:2])
    if vtype == "raidz3":
        return sum(sizes) - sum(sorted(sizes)[:3])
    return sum(sizes)


def _normalize_vdev_type(name: str, vtype: str) -> str:
    """Concrete topology for group vdevs that ``zpool status -j`` reports
    generically.  RAIDZ entries have ``vdev_type`` ``raidz`` and carry their
    parity count in the vdev name (``raidz2-0``); the UI and recreate specs
    need it spelled out as a valid topology."""
    if vtype == "raidz":
        prefix = (name or "").split("-")[0]
        if prefix.startswith("raidz"):
            return prefix
    return vtype


def _physical_sector_bytes(path_or_name: str) -> Optional[int]:
    """Physical sector size in bytes of the block device behind a ZFS leaf.

    Reads ``/sys/block/<base>/queue/physical_block_size`` for the kernel block
    device resolved from a leaf's path or name (e.g. ``sda1``/by-id symlink).
    Returns None when the device is not present or unreadable.  ZFS's per-leaf
    ``ashift`` reflects the *logical* sector size on 512e disks, so the vdev
    standard (physical sector size) is sourced here instead.
    """
    try:
        base = normalize_base_name(path_or_name)
        with open(f"/sys/block/{base}/queue/physical_block_size") as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def _compute_usable_bytes(data_vdevs: Optional[List[Dict[str, Any]]]) -> float:
    """Total usable capacity of a pool's data vdevs after redundancy."""
    if not data_vdevs:
        return 0.0
    return sum(_vdev_usable_bytes(v) for v in data_vdevs)


def _atime_to_params(value: str) -> List[str]:
    """Map a UI atime choice to ZFS -o property tokens.

    ``none`` disables access-time updates; ``all`` updates on every read
    (atime on + relatime off); ``partial`` only when older than mtime/ctime
    (atime on + relatime on).
    """
    value = (value or "partial").strip().lower()
    if value == "none":
        return ["atime=off", "relatime=off"]
    if value == "all":
        return ["atime=on", "relatime=off"]
    return ["atime=on", "relatime=on"]


def _params_to_atime(atime: Optional[str], relatime: Optional[str]) -> str:
    """Inverse of _atime_to_params: derive the UI value from ZFS properties."""
    a = (atime or "").strip().lower()
    r = (relatime or "").strip().lower()
    if a == "off":
        return "none"
    if r == "off":
        return "all"
    return "partial"


class ZfsManager:
    """Manages ZFS pools and datasets (live state; ZFS is the source of truth).

    Cross-domain destruction orchestration (which also tears down NFS/SMB
    shares) lives in :class:`nazman.services.destruction.DestructionService`.
    """

    # ── Pool operations ────────────────────────────────────────────────

    async def list_pools(self, db: Session) -> List[Dict[str, Any]]:
        """List all ZFS pools with live status from ZFS."""
        try:
            stdout, stderr, returncode = await run_zpool(
                "list", "-p", "-H", "-o", "name,size,allocated,free,capacity,health",
                op="read",
            )

            if returncode != 0:
                raise PoolError(f"Failed to list pools: {stderr}")

            pools = []
            for line in stdout.strip().split('\n'):
                if not line:
                    continue

                parts = line.split()
                if len(parts) >= 6:
                    pool_name = parts[0]

                    pool = self.get_pool_by_name(db, pool_name)
                    if not pool:
                        pool = Pool(name=pool_name)
                        db.add(pool)
                        db.commit()
                        db.refresh(pool)

                    status_info = await self.get_pool_status(pool_name)

                    size_bytes = int(parts[1]) if parts[1].isdigit() else None
                    allocated_bytes = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                    free_bytes = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
                    capacity = parts[4].rstrip("%") if len(parts) > 4 and parts[4] else None
                    health = parts[5] if len(parts) > 5 else "ONLINE"

                    usable_bytes = _compute_usable_bytes(status_info.get("data_vdevs"))
                    compression = await self._get_pool_compressratio(pool_name)

                    pools.append({
                        "id": pool.id,
                        "name": pool_name,
                        "status": status_info.get("status", "ONLINE"),
                        "topology": status_info.get("topology", "stripe"),
                        "health": health,
                        "size_bytes": size_bytes,
                        "allocated_bytes": allocated_bytes,
                        "free_bytes": free_bytes,
                        "usable_bytes": int(usable_bytes) if usable_bytes else None,
                        "used_capacity_pct": float(capacity) if capacity and capacity.isdigit() else None,
                        "compressratio": compression,
                        "datasets": await self.list_datasets(db, pool_name),
                        "created_at": pool.created_at.isoformat() if pool.created_at else None,
                    })

            return pools

        except Exception as e:
            if isinstance(e, PoolError):
                raise
            raise PoolError(f"Error listing pools: {str(e)}")

    def get_pool_by_name(self, db: Session, name: str) -> Optional[Pool]:
        """The Pool row for a pool name, or None."""
        return db.query(Pool).filter(Pool.name == name).first()

    def list_pool_names(self, db: Session) -> List[str]:
        """Names of all known pools (DB-tracked)."""
        return [p.name for p in db.query(Pool).all()]

    async def is_disk_in_pool(self, by_id: str) -> Optional[str]:
        """Check if a disk (by its /dev/disk/by-id path) is a member of any imported pool.

        Returns the pool name if found, None otherwise.
        """
        if not by_id or not by_id.startswith("/dev/disk/by-id/"):
            return None
        members = await self.get_pool_members()
        return zfs_query.pool_member_for_by_id(members, by_id)

    @staticmethod
    def pool_member_for_disk(pool_members: Dict[str, str], disk: Disk) -> Optional[str]:
        """Return the name of the pool that owns ``disk`` (whole disk or any of its partitions)."""
        return zfs_query.pool_member_for_disk(pool_members, disk)

    @staticmethod
    def pools_for_disk(pool_members: Dict[str, str], disk: Disk) -> List[str]:
        """Every distinct pool that owns ``disk`` (whole disk or any of its partitions)."""
        return zfs_query.pools_for_disk(pool_members, disk)

    @staticmethod
    def pool_member_for_by_id(pool_members: Dict[str, str], by_id: Optional[str]) -> Optional[str]:
        """Return the pool owning ``by_id`` (exact, basename, or any -partN child)."""
        return zfs_query.pool_member_for_by_id(pool_members, by_id)

    @staticmethod
    def _iter_vdev_disks(vdev: Dict[str, Any]):
        """Yield (leaf, path, name) for every leaf disk entry in a vdev tree."""
        kind = vdev.get("type") or vdev.get("vdev_type")
        if kind == "disk":
            yield vdev, vdev.get("path"), vdev.get("name")
        children = vdev.get("vdevs", {})
        if isinstance(children, dict):
            for child in children.values():
                yield from ZfsManager._iter_vdev_disks(child)
        elif isinstance(children, list):
            for child in children:
                yield from ZfsManager._iter_vdev_disks(child)

    @staticmethod
    def _disk_identity_keys(by_id: Optional[str]) -> List[str]:
        """Exact/basename ids whose (or whose ``-partN`` children's) vdevs belong to a disk."""
        if not by_id:
            return []
        return [by_id, by_id.rsplit("/", 1)[-1]]

    @staticmethod
    def _leaf_matches_disk(key: str, ids: List[str]) -> bool:
        """True if a vdev/event key identifies one of ``ids`` (whole or a -partN child)."""
        return any(key == i or key.startswith(f"{i}-part") for i in ids)

    async def get_pool_members(self) -> Dict[str, str]:
        """Map every leaf device used by imported pools to its pool name.

        Keys are the device paths reported by ``zpool status -j`` (stable
        by-id whole-disk paths or ``-partN`` partition paths), so partitioned
        pool members are matched exactly rather than via whole-disk identity.
        """
        members: Dict[str, str] = {}
        try:
            stdout, _, rc = await run_zpool("status", "-j", check=False, op="read")
            if rc != 0:
                return members
            data = json.loads(stdout)
            pools = data.get("pools", {})
            if not isinstance(pools, dict):
                return members
            for pool_name, pool_data in pools.items():
                vdevs = pool_data.get("vdevs", {})
                for vdev in vdevs.values():
                    for leaf, path, name in self._iter_vdev_disks(vdev):
                        members.setdefault(path or name, pool_name)
            return members
        except Exception:
            return {}

    async def get_members_and_errors(self) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        """Pool member map + per-leaf error counters from a single ``zpool status -j``.

        Combines :meth:`get_pool_members` and :meth:`get_pool_error_counts` into
        one subprocess call and one parse, for callers that need both views
        (e.g. the disks page).  Mirrors their self-defensive behaviour: returns
        ``({}, {})`` on failure.
        """
        members: Dict[str, str] = {}
        counts: Dict[str, Dict[str, Any]] = {}
        try:
            stdout, _, rc = await run_zpool("status", "-j", check=False, op="read")
            if rc != 0:
                return members, counts
            data = json.loads(stdout)
            pools = data.get("pools", {})
            if not isinstance(pools, dict):
                return members, counts
            for pool_name, pool_data in pools.items():
                vdevs = pool_data.get("vdevs", {})
                if not isinstance(vdevs, dict):
                    continue
                for vdev in vdevs.values():
                    for leaf, path, name in self._iter_vdev_disks(vdev):
                        key = path or name
                        if not key:
                            continue
                        members.setdefault(key, pool_name)
                        counts.setdefault(key, {
                            "pool": pool_name,
                            "read": ZfsManager._leaf_counter(leaf, "read"),
                            "write": ZfsManager._leaf_counter(leaf, "write"),
                            "cksum": ZfsManager._leaf_counter(leaf, "cksum"),
                            "guid": leaf.get("guid"),
                        })
        except Exception:
            return {}, {}
        return members, counts

    async def get_pool_error_counts(self) -> Dict[str, Dict[str, Any]]:
        """Per-leaf ZFS read/write/checksum error counters for all pools.

        One ``zpool status -j`` call.  Keys are the same live device paths used
        by :meth:`get_pool_members`.  Counters are cumulative since the pool was
        last cleared/imported, so they persist across reboots.
        """
        counts: Dict[str, Dict[str, Any]] = {}
        try:
            stdout, _, rc = await run_zpool("status", "-j", check=False, op="read")
            if rc != 0:
                return counts
            data = json.loads(stdout)
            pools = data.get("pools", {})
            if not isinstance(pools, dict):
                return counts
            for pool_name, pool_data in pools.items():
                vdevs = pool_data.get("vdevs", {})
                if not isinstance(vdevs, dict):
                    continue
                for vdev in vdevs.values():
                    for leaf, path, name in self._iter_vdev_disks(vdev):
                        key = path or name
                        if not key:
                            continue
                        counts.setdefault(key, {
                            "pool": pool_name,
                            "read": ZfsManager._leaf_counter(leaf, "read"),
                            "write": ZfsManager._leaf_counter(leaf, "write"),
                            "cksum": ZfsManager._leaf_counter(leaf, "cksum"),
                            "guid": leaf.get("guid"),
                        })
            return counts
        except Exception:
            return {}

    @staticmethod
    def _leaf_counter(leaf: Dict[str, Any], key: str) -> int:
        try:
            return int(leaf.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def pool_errors_for_disk(
        counts: Dict[str, Dict[str, Any]], disk: Disk
    ) -> Optional[Dict[str, int]]:
        """Aggregate ZFS read/write/checksum counters for one disk, or None."""
        if disk is None:
            return None
        ids = ZfsManager._disk_identity_keys(disk.by_id)
        totals = {"read": 0, "write": 0, "cksum": 0}
        found = False
        for key, info in counts.items():
            if ZfsManager._leaf_matches_disk(key, ids):
                found = True
                totals["read"] += ZfsManager._leaf_counter(info, "read")
                totals["write"] += ZfsManager._leaf_counter(info, "write")
                totals["cksum"] += ZfsManager._leaf_counter(info, "cksum")
        return totals if found else None

    @staticmethod
    def leaf_identities_for_disk(
        counts: Dict[str, Dict[str, Any]], disk: Disk
    ) -> Dict[str, set]:
        """GUIDs/paths of the vdev leaves that belong to a disk, for event matching."""
        ids = ZfsManager._disk_identity_keys(disk.by_id if disk else None)
        guids: set = set()
        paths: set = set()
        for key, info in counts.items():
            if ZfsManager._leaf_matches_disk(key, ids):
                guid = info.get("guid")
                if guid is not None:
                    guids.add(str(guid))
                if key.startswith("/"):
                    paths.add(key)
        return {"guids": guids, "paths": paths}

    ZFS_ERROR_CLASSES = {
        "checksum", "data", "io", "io_failure", "vdev.corrupt_data", "dio_verify_rd",
    }

    async def get_pool_error_events(self, pool_name: str) -> List[Dict[str, Any]]:
        """Timestamped ZFS error events (checksum/data/io) for a pool.

        Reads the pool's recent event log via ``zpool events <pool> -v -H``
        (wordy text form; ``zpool events`` has no ``-j`` on OpenZFS 2.x).
        Events are a ring buffer kept since the pool was imported/cleared, so
        only recent activity is available.
        """
        events: List[Dict[str, Any]] = []
        try:
            stdout, _, rc = await run_zpool(
                "events", pool_name, "-v", "-H", check=False, op="read",
            )
            if rc != 0:
                return events
            current: Optional[Dict[str, Any]] = None
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                if not line[:1].isspace() and "\t" in line:
                    timestamp, cls = line.split("\t", 1)
                    current = {"time": timestamp.strip(), "class": cls.strip()}
                    events.append(current)
                    continue
                if current is None or " = " not in line:
                    continue
                key, _, value = line.partition(" = ")
                key = key.strip()
                if key in ("vdev_guid", "vdev_path", "vdev_devid"):
                    current.setdefault(key, value.strip())
            return [
                e for e in events
                if e["class"].rsplit(".", 1)[-1] in ZfsManager.ZFS_ERROR_CLASSES
            ]
        except Exception:
            return []

    @staticmethod
    def events_for_disk(
        events: List[Dict[str, Any]], identities: Dict[str, set]
    ) -> List[Dict[str, Any]]:
        """Filter error events down to those touching a disk's vdev leaves.

        Matches on ``vdev_guid`` or ``vdev_path`` (the leaf's full device path,
        which may be a ``-partN`` child path).  Newest first.
        """
        guids = identities.get("guids") or set()
        ids = [p for p in (identities.get("paths") or set())]
        out = []
        for ev in events:
            guid = str(ev.get("vdev_guid") or "")
            path = ev.get("vdev_path") or ""
            if guid and guid in guids:
                out.append(ev)
            elif path and ZfsManager._leaf_matches_disk(path, ids):
                out.append(ev)
        out.sort(key=lambda e: e.get("time") or "", reverse=True)
        return out

    async def get_pool_status(self, pool_name: str) -> Dict[str, Any]:
        """Get detailed pool status from ZFS."""
        validate_pool_name(pool_name)

        try:
            stdout, stderr, returncode = await run_zpool(
                "status", "-j", pool_name, op="read",
            )

            if returncode != 0:
                raise PoolError(f"Failed to get pool status: {stderr}")

            data = json.loads(stdout)
            pools = data.get("pools")
            pool_info = {}
            if isinstance(pools, dict) and pools:
                pool_info = next(iter(pools.values()))
            elif isinstance(pools, list) and pools:
                pool_info = pools[0]
            elif isinstance(data, dict) and (data.get("pool") or data.get("config")):
                # Ambiguity-sensitive: choose the sub-dict that actually holds vdevs.
                for candidate in (data.get("pool"), data.get("config"), data):
                    if isinstance(candidate, dict) and candidate.get("vdevs") is not None:
                        pool_info = candidate
                        break
                else:
                    pool_info = data

            topology = "stripe"
            data_vdevs = []
            special_vdevs = []
            log_vdevs = []
            cache_vdevs = []

            def _leaf_disks(children_raw):
                """Extract leaf disk entries directly under a vdev's vdevs dict."""
                out = []
                if isinstance(children_raw, dict):
                    for dname, disk in children_raw.items():
                        if disk.get("vdev_type") == "disk":
                            out.append({
                                "name": disk.get("name", dname),
                                "state": disk.get("state", "UNKNOWN"),
                                "path": disk.get("path", ""),
                                "size": disk.get("rep_dev_size") or disk.get("phys_space") or disk.get("size") or "",
                                "guid": disk.get("guid", ""),
                            })
                return out

            def _all_leaf(children_raw):
                """True if every entry under children_raw is a leaf disk."""
                if not isinstance(children_raw, dict) or not children_raw:
                    return False
                return all(
                    isinstance(v, dict) and v.get("vdev_type") == "disk"
                    for v in children_raw.values()
                )

            def _collect_vdevs(vdev_dict, class_override=None):
                """Recursively collect vdevs from a dict (zpool status -j format)."""
                if not isinstance(vdev_dict, dict):
                    return
                for name, vdev in vdev_dict.items():
                    vtype = vdev.get("vdev_type", "")
                    vclass = class_override or vdev.get("class", "") or ""
                    children_raw = vdev.get("vdevs", {})

                    children = _leaf_disks(children_raw)

                    entry = {
                        "name": name,
                        "type": _normalize_vdev_type(name, vtype),
                        "class": vclass,
                        "guid": vdev.get("guid", ""),
                        "children": children,
                        **{k: v for k, v in vdev.items() if k in ("total_space", "state", "alloc_space")},
                    }

                    if vtype == "disk":
                        continue
                    elif "special" in str(vclass):
                        special_vdevs.append(entry)
                    elif "log" in str(vclass):
                        log_vdevs.append(entry)
                    elif "cache" in str(vclass):
                        cache_vdevs.append(entry)
                    elif vtype == "root":
                        if _all_leaf(children_raw):
                            # Root directly holds bare disks (simple stripe) - treat as a data vdev
                            data_vdevs.append(entry)
                        else:
                            _collect_vdevs(children_raw, class_override)
                    else:
                        data_vdevs.append(entry)
                        _collect_vdevs(children_raw, class_override)

            # Parse main vdevs tree
            vdevs_raw = pool_info.get("vdevs", {})
            if isinstance(vdevs_raw, dict):
                _collect_vdevs(vdevs_raw)

            # Parse special/log/cache (separate top-level keys in zpool status -j)
            for class_key, target in [("special", special_vdevs), ("log", log_vdevs), ("cache", cache_vdevs)]:
                class_dict = pool_info.get(class_key, {})
                if isinstance(class_dict, dict):
                    for name, vdev in class_dict.items():
                        vtype = vdev.get("vdev_type", "")
                        children_raw = vdev.get("vdevs", {})
                        children = _leaf_disks(children_raw)
                        target.append({"name": name, "type": _normalize_vdev_type(name, vtype), "class": class_key, "guid": vdev.get("guid", ""), "children": children, **{k: v for k, v in vdev.items() if k in ("total_space", "state", "alloc_space")}})

            # Determine topology from data vdevs
            if data_vdevs:
                topology = data_vdevs[0].get("type", "stripe") or "stripe"
                if topology == "root":
                    topology = "stripe"

            status_str = pool_info.get("state", "ONLINE").upper()

            # Per-vdev sector size exponent (ashift).  Requires
            # ``zpool get ... all-vdevs``, i.e. OpenZFS >= 2.2; the install
            # path (prepare.sh) gates on that version, so this only fails to
            # fill on systems that bypassed the check.
            ashifts: Dict[str, int] = {}
            try:
                ashifts = await self._get_vdev_ashifts(pool_name)
            except Exception:
                pass

            def _ashift_for(guid: str, name: str) -> Optional[int]:
                for key in (guid, name):
                    value = ashifts.get(key)
                    if value is not None:
                        return value
                return None

            all_groups = data_vdevs + special_vdevs + log_vdevs + cache_vdevs
            for group in all_groups:
                group["ashift"] = _ashift_for(str(group.get("guid") or ""), str(group.get("name") or ""))
                child_phys = []
                for child in group.get("children", []):
                    child["ashift"] = _ashift_for(str(child.get("guid") or ""), str(child.get("name") or ""))
                    phys = _physical_sector_bytes(str(child.get("path") or child.get("name") or ""))
                    if phys:
                        child["physical_sector_size"] = phys
                        child_phys.append(phys)
                if child_phys:
                    group["physical_sector_size"] = max(child_phys)

            pool_ashift = await self._get_pool_ashift(pool_name)

            return {
                "name": pool_name,
                "status": status_str,
                "topology": topology,
                "vdevs": all_groups,
                "data_vdevs": data_vdevs,
                "special_vdevs": special_vdevs,
                "log_vdevs": log_vdevs,
                "cache_vdevs": cache_vdevs,
                "scan": pool_info.get("scan", {}),
                "config": pool_info.get("config", {}),
                "ashift": pool_ashift or None,
                "sector_size_bytes": (1 << pool_ashift) if pool_ashift else None,
            }

        except Exception as e:
            if isinstance(e, PoolError):
                raise
            raise PoolError(f"Error getting pool status: {str(e)}")

    async def _get_pool_ashift(self, pool_name: str) -> int:
        """Pool-level ashift as an int (0 when ZFS reports the default)."""
        stdout, _, rc = await run_zpool(
            "get", "-Hp", "-o", "value", "ashift", pool_name, check=False, op="read",
        )
        if rc != 0:
            return 0
        try:
            return int(stdout.strip())
        except ValueError:
            return 0

    async def _get_vdev_ashifts(self, pool_name: str) -> Dict[str, int]:
        """Map every vdev's identity (guid, then name) to its ashift.

        Uses ``zpool get -o name,property,value ashift,guid <pool> all-vdevs``
        (OpenZFS >= 2.2), which reports each vdev's own ashift, including
        leaves that differ from the pool's ``ashift`` property.  An empty dict
        is returned when the per-vdev form is unavailable.
        """
        stdout, _, rc = await run_zpool(
            "get", "-Hp", "-o", "name,property,value", "ashift,guid",
            pool_name, "all-vdevs", check=False, op="read",
        )
        if rc != 0:
            return {}
        ashift_by_name: Dict[str, int] = {}
        guid_by_name: Dict[str, str] = {}
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, prop, value = parts
            if prop == "ashift" and value.isdigit():
                ashift_by_name[name] = int(value)
            elif prop == "guid" and value:
                guid_by_name[name] = value
        result: Dict[str, int] = {}
        for name, ashift in ashift_by_name.items():
            result[guid_by_name.get(name, name)] = ashift
        return result

    async def _slot_uuid_map(self, db: Session) -> Dict[str, Dict[str, Any]]:
        """Map every present partition's by-id path to its slot identity.

        Values are ``{slot_uuid, size_bytes, partition_number}`` so a rebuild can
        reproduce the exact ``nazman:<uuid>`` GPT layout before pool create.
        """
        disks = db.query(Disk).all()
        live = [get_device_path(d) for d in disks]
        live = [p for p in live if p]
        info = await read_slot_uuids(live)
        result: Dict[str, Dict[str, Any]] = {}
        for disk in disks:
            path = get_device_path(disk)
            if not path:
                continue
            for part in info.get(path, {}).get("partitions", []):
                m = re.search(r"(\d+)$", part.get("name") or "")
                if not m or not part.get("slot_uuid"):
                    continue
                number = int(m.group(1))
                dev = partition_by_id(disk.by_id, number)
                if dev:
                    result[dev] = {
                        "slot_uuid": part["slot_uuid"],
                        "size_bytes": part.get("size_bytes", 0),
                        "partition_number": number,
                    }
        return result

    @staticmethod
    def _resolve_disk_for_leaf(db: Session, leaf: str) -> Optional[Disk]:
        """Resolve a zpool-reported leaf device to its ``Disk`` row."""
        if not leaf:
            return None
        if leaf.startswith("/dev/disk/by-id/"):
            m = re.search(r"-part(\d+)$", leaf)
            base = leaf[:m.start()] if m else leaf
            return db.query(Disk).filter(Disk.by_id == base).first()
        # Bare by-id basename (zpool drops the directory prefix).
        disk = db.query(Disk).filter(Disk.by_id == f"/dev/disk/by-id/{leaf}").first()
        if disk:
            return disk
        # Kernel name (e.g. sda1, nvme0n1p2): match against the live device map.
        base = kernel_base_name(leaf)
        for d in db.query(Disk).all():
            path = get_device_path(d)
            if not path:
                continue
            if path == f"/dev/{leaf}" or kernel_base_name(path.rsplit("/", 1)[-1]) == base:
                return d
        return None

    async def get_pool_recreate_specs(self, db: Session) -> List[Dict[str, Any]]:
        """Pool/vdev topology needed to recreate every imported pool.

        Each pool is ``{name, ashift, vdevs:[{role, topology, ashift,
        devices:[{by_id, serial, size_bytes, slot_uuid, partition_number,
        partition_size_bytes}]}]}``.  Device identity is stored as by-id +
        serial + size so a moved disk can be matched on new hardware even if its
        by-id path changes.  Partitioned vdevs also carry the slot UUID and the
        partition's size so the GPT layout can be reproduced on the new disk.
        """
        slot_map = await self._slot_uuid_map(db)
        specs: List[Dict[str, Any]] = []
        for pool_name in self.list_pool_names(db):
            try:
                status = await self.get_pool_status(pool_name)
            except Exception:
                continue
            ashift = await self._get_pool_ashift(pool_name)
            vdevs: List[Dict[str, Any]] = []
            for role, key in (
                ("data", "data_vdevs"), ("special", "special_vdevs"),
                ("log", "log_vdevs"), ("cache", "cache_vdevs"),
            ):
                for vdev in status.get(key, []):
                    topology = vdev.get("type") or "stripe"
                    if topology in ("root", ""):
                        topology = "stripe"
                    devices = []
                    for child in vdev.get("children", []):
                        leaf = child.get("path") or child.get("name") or ""
                        disk = self._resolve_disk_for_leaf(db, leaf)
                        if not disk:
                            continue
                        slot = slot_map.get(leaf) if leaf.startswith("/") else None
                        devices.append({
                            "by_id": disk.by_id,
                            "serial": disk.serial,
                            "size_bytes": disk.size_bytes,
                            "slot_uuid": slot["slot_uuid"] if slot else None,
                            "partition_number": slot["partition_number"] if slot else None,
                            "partition_size_bytes": slot["size_bytes"] if slot else None,
                        })
                    if devices:
                        vdevs.append({
                            "role": role, "topology": topology,
                            "ashift": ashift, "devices": devices,
                        })
            specs.append({"name": pool_name, "ashift": ashift, "vdevs": vdevs})
        return specs

    async def get_dataset_recreate_specs(self, db: Session) -> List[Dict[str, Any]]:
        """Dataset names, owning pool, mountpoint and recreatable properties."""
        _PROP_KEYS = (
            "compression", "recordsize", "sync_mode", "quota",
            "special_small_blocks", "atime", "canmount", "readonly",
        )
        specs: List[Dict[str, Any]] = []
        for ds in await self.list_datasets(db):
            name = ds.get("name")
            if not name or "/" not in name:
                continue
            specs.append({
                "name": name,
                "pool": name.split("/", 1)[0],
                "mountpoint": ds.get("mountpoint"),
                "properties": {k: ds[k] for k in _PROP_KEYS if k in ds},
            })
        return specs

    async def get_dataset_spec(self, dataset_name: str) -> Dict[str, Any]:
        """Single dataset's recreate spec (name, pool, mountpoint, properties)."""
        props = await self.get_dataset_properties(dataset_name)
        mountpoint = None
        stdout, _, rc = await run_zfs(
            "get", "-Hp", "-o", "value", "mountpoint", dataset_name, check=False, op="read",
        )
        if rc == 0:
            value = stdout.strip()
            if value and value not in ("-", "none"):
                mountpoint = value
        return {
            "name": dataset_name,
            "pool": dataset_name.split("/", 1)[0],
            "mountpoint": mountpoint,
            "properties": props,
        }

    async def _get_pool_compressratio(self, pool_name: str) -> Optional[float]:
        """Return the pool root dataset's compression ratio (e.g. 1.83)."""
        try:
            stdout, _, rc = await run_zfs(
                "get", "-H", "-o", "value", "compressratio", pool_name,
                check=False, op="read",
            )
            if rc != 0:
                return None
            value = stdout.strip()
            if not value or value in ("-", "1.00x"):
                return None
            return float(value.rstrip("x"))
        except Exception:
            return None

    async def _resolve_devices(
        self, db: Session, device_specs: List[Dict[str, Any]]
    ) -> List[str]:
        """Resolve a list of device specs to device paths.

        Each spec is ``{"disk_id": int, "slot_uuid": str | None}``.
        If ``slot_uuid`` is provided, the partition with that UUID is used.
        Otherwise the whole disk by-id path is used.
        """
        if not device_specs:
            return []

        # Collect all disk IDs and batch-read their GPT partition names
        disk_ids = {spec["disk_id"] for spec in device_specs}
        disks = db.query(Disk).filter(Disk.id.in_(disk_ids)).all()
        disk_map = {d.id: d for d in disks}

        # Batch-read GPT names for the live kernel paths of the present disks.
        disk_paths = []
        for d in disks:
            p = get_device_path(d)
            if p:
                disk_paths.append(p)
        slot_info = await read_slot_uuids(disk_paths)

        devices = []
        for spec in device_specs:
            disk_id = spec["disk_id"]
            slot_uuid = spec.get("slot_uuid")

            disk = disk_map.get(disk_id)
            if not disk:
                raise ValidationError(f"Disk {disk_id} not found")

            live_path = get_device_path(disk)
            label = get_device_name(disk) or disk.model or disk.serial or str(disk.id)

            if slot_uuid:
                # Resolve partition by slot UUID using the live kernel path
                if not live_path:
                    raise ValidationError(
                        f"Disk {label} is not currently present; cannot resolve slot {slot_uuid}"
                    )
                parts = slot_info.get(live_path, {}).get("partitions", [])
                dev_path = resolve_slot_to_device(disk.by_id, slot_uuid, parts)
                if not dev_path:
                    raise ValidationError(
                        f"Partition with slot UUID {slot_uuid} not found on {label}"
                    )
                devices.append(dev_path)
            else:
                # Whole disk — use by-id path as the canonical identifier
                if not disk.by_id:
                    raise ValidationError(
                        f"Disk {label} has no by-id path; cannot use as whole disk"
                    )
                devices.append(disk.by_id)

        return devices

    async def create_pool(
        self,
        db: Session,
        name: str,
        vdevs: List[Dict[str, Any]],
        ashift: int = 12
    ) -> Dict[str, Any]:
        """Create a new ZFS pool from inline vdev specs.

        Each vdev spec::

            {
                "role": "data" | "log" | "cache" | "special",
                "topology": "stripe" | "mirror" | "raidz1" | "raidz2" | "raidz3",
                "devices": [{"disk_id": int, "slot_uuid": str | None}, ...]
            }
        """
        name = validate_pool_name(name)

        # Check if pool already exists
        existing = self.get_pool_by_name(db, name)
        if existing:
            list_out, _, list_rc = await run_zpool(
                "list", "-H", "-o", "name", name, check=False, op="read",
            )
            if list_rc == 0 and name in list_out.split():
                raise ValidationError(f"Pool '{name}' already exists")
            db.delete(existing)
            db.commit()

        if not vdevs:
            raise ValidationError("At least one vdev is required")

        # Validate roles
        valid_roles = {"data", "log", "cache", "special"}
        for vdev in vdevs:
            role = vdev.get("role")
            if role not in valid_roles:
                raise ValidationError(f"Invalid vdev role '{role}'; must be one of {valid_roles}")
            if not vdev.get("devices"):
                raise ValidationError(f"Vdev '{role}' has no devices")

        data_vdevs = [v for v in vdevs if v["role"] == "data"]
        if not data_vdevs:
            raise ValidationError("At least one data vdev is required")

        # A single zpool create can only specify one pool-level ashift. Groups that
        # request a different ashift from the data vdevs must be added afterwards
        # with zpool add -o ashift=N, which does support a per-vdev ashift.
        data_ashift = next((v.get("ashift") for v in data_vdevs if v.get("ashift")), ashift)

        # Resolve devices once per vdev, keyed by vdev id, for reuse across steps.
        resolved = {}
        for vdev in vdevs:
            resolved[id(vdev["devices"])] = await self._resolve_devices(db, vdev["devices"])

        def group_ashift(group):
            return next((v.get("ashift") for v in group if v.get("ashift")), None)

        # Groups needing a separate zpool add with their own ashift.
        add_steps = []  # list of (label, ashift, command_args)

        create_cmd = ["create", "-f", "-o", f"ashift={data_ashift}", name]

        # Data vdevs always go in the create command.
        for vdev in data_vdevs:
            devices = resolved[id(vdev["devices"])]
            topology = vdev["topology"]
            if topology == "stripe":
                create_cmd.extend(devices)
            else:
                create_cmd.extend([topology] + devices)

        for role in ("special", "log", "cache"):
            group = [v for v in vdevs if v["role"] == role]
            if not group:
                continue
            g_ashift = group_ashift(group)
            if g_ashift is None or g_ashift == data_ashift:
                # Same ashift -> keep in the single create command.
                create_cmd.append(role)
                for vdev in group:
                    devices = resolved[id(vdev["devices"])]
                    topology = vdev["topology"]
                    if role == "cache" or topology == "stripe" or len(devices) == 1:
                        create_cmd.extend(devices)
                    else:
                        create_cmd.extend([topology] + devices)
            else:
                # Different ashift -> add via zpool add after the pool exists.
                add_cmd = ["add", "-f", "-o", f"ashift={g_ashift}", name, role]
                for vdev in group:
                    devices = resolved[id(vdev["devices"])]
                    topology = vdev["topology"]
                    if role == "cache" or topology == "stripe" or len(devices) == 1:
                        add_cmd.extend(devices)
                    else:
                        add_cmd.extend([topology] + devices)
                add_steps.append((role, g_ashift, add_cmd))

        # Create the pool
        stdout, stderr, returncode = await run_zpool(*create_cmd, timeout=600)

        if returncode != 0:
            raise PoolError(f"Failed to create pool: {stderr}")

        # Add any vdevs that required a different ashift.
        for label, g_ashift, add_cmd in add_steps:
            stdout, stderr, returncode = await run_zpool(*add_cmd, timeout=600)
            if returncode != 0:
                raise PoolError(
                    f"Pool created but failed to add {label} vdev (ashift={g_ashift}): {stderr}"
                )

        # Create minimal database record
        pool = Pool(name=name)
        db.add(pool)
        db.commit()
        db.refresh(pool)

        return {
            "id": pool.id,
            "name": pool.name,
            "created_at": pool.created_at.isoformat() if pool.created_at else None,
        }

    async def remove_device(
        self,
        db: Session,
        pool_name: str,
        device_path: str
    ) -> Dict[str, Any]:
        """Remove a device from a pool (only for log/cache devices)."""
        validate_pool_name(pool_name)

        pool = self.get_pool_by_name(db, pool_name)
        if not pool:
            raise PoolNotFoundError(f"Pool '{pool_name}' not found")

        status_info = await self.get_pool_status(pool_name)
        device_type = None
        for vdev in status_info.get("vdevs", []):
            vdev_type = vdev.get("type", "")
            if vdev_type in ("log", "cache"):
                for child in vdev.get("children", []):
                    if child.get("name") == device_path:
                        device_type = vdev_type
                        break

        if not device_type:
            raise PoolError(f"Device '{device_path}' not found in pool '{pool_name}'")

        if device_type not in ["log", "cache"]:
            raise PoolError("Only log and cache devices can be removed")

        stdout, stderr, returncode = await run_zpool(
            "remove", pool_name, device_path,
            timeout=300
        )

        if returncode != 0:
            raise PoolError(f"Failed to remove device: {stderr}")

        return {"name": pool_name, "removed": device_path}

    async def scrub_pool(self, pool_name: str) -> None:
        """Start a scrub on a pool."""
        validate_pool_name(pool_name)

        stdout, stderr, returncode = await run_zpool(
            "scrub", pool_name,
            timeout=3600
        )

        if returncode != 0:
            raise PoolError(f"Failed to start scrub: {stderr}")

    async def export_pool(self, pool_name: str) -> None:
        """Export a pool."""
        validate_pool_name(pool_name)

        stdout, stderr, returncode = await run_zpool(
            "export", pool_name,
            timeout=300
        )

        if returncode != 0:
            raise PoolError(f"Failed to export pool: {stderr}")

    async def import_pool(self, pool_name: str) -> None:
        """Import a pool."""
        validate_pool_name(pool_name)

        stdout, stderr, returncode = await run_zpool(
            "import", pool_name,
            timeout=300
        )

        if returncode != 0:
            raise PoolError(f"Failed to import pool: {stderr}")

    # ── Dataset operations ─────────────────────────────────────────────

    async def list_datasets(self, db: Session, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all datasets with live ZFS properties (batched per pool)."""
        try:
            cmd = ["list", "-H", "-o", "name,used,available,referenced,mountpoint,creation",
                   "-t", "filesystem"]
            if pool_name:
                cmd.extend(["-r", pool_name])

            stdout, stderr, returncode = await run_zfs(*cmd, check=False, op="read")

            if returncode != 0:
                raise DatasetError(f"Failed to list datasets: {stderr}")

            raw_lines = [line for line in stdout.strip().split('\n') if line]
            pool_names_seen = set()
            batch_props: Dict[str, Dict[str, str]] = {}
            for line in raw_lines:
                parts = line.split('\t')
                if len(parts) >= 6:
                    ds_pool = parts[0].split('/')[0]
                    if ds_pool not in pool_names_seen:
                        pool_names_seen.add(ds_pool)
                        batch_props.update(await self.get_all_dataset_properties(ds_pool))

            # Collect pool root dataset names to exclude them
            pool_root_names = set(self.list_pool_names(db))

            datasets = []
            for line in raw_lines:
                parts = line.split('\t')
                if len(parts) < 6:
                    continue

                name = parts[0]

                # Skip pool root datasets (e.g. "lib1") — these are pools, not datasets
                if name in pool_root_names:
                    continue

                live_props = batch_props.get(name, {})
                datasets.append({
                    "name": name,
                    "mountpoint": parts[4] if parts[4] != '-' else None,
                    "used": parts[1] if parts[1] != '-' else None,
                    "available": parts[2] if parts[2] != '-' else None,
                    "referenced": parts[3] if parts[3] != '-' else None,
                    "created_at": parts[5] if parts[5] != '-' else None,
                    **live_props,
                })

            return datasets

        except Exception as e:
            if isinstance(e, DatasetError):
                raise
            raise DatasetError(f"Error listing datasets: {str(e)}")

    async def dataset_exists(self, dataset_name: str) -> bool:
        """Confirm a dataset currently exists in ZFS by its full name."""
        return await zfs_query.dataset_exists(dataset_name)

    async def get_dataset_properties(self, dataset_name: str) -> Dict[str, str]:
        """Get live ZFS properties for a single dataset (1 subprocess call)."""
        try:
            stdout, stderr, rc = await run_zfs(
                "get", "-H", "-o", "property,value",
                "compression,recordsize,sync,quota,special_small_blocks,"
                "atime,relatime,canmount,readonly",
                dataset_name, check=False, op="read",
            )
            if rc != 0:
                return {}
            atime = None
            relatime = None
            props = {}
            for line in stdout.strip().split('\n'):
                if not line or '\t' not in line:
                    continue
                prop, value = line.split('\t', 1)
                if value == '-':
                    continue
                if prop == 'sync':
                    prop = 'sync_mode'
                if prop == 'atime':
                    atime = value
                    continue
                if prop == 'relatime':
                    relatime = value
                    continue
                props[prop] = value
            if atime is not None:
                props["atime"] = _params_to_atime(atime, relatime)
            return props
        except Exception:
            return {}

    async def get_all_dataset_properties(self, pool_name: str) -> Dict[str, Dict[str, str]]:
        """Get live ZFS properties for all datasets in a pool (1 subprocess call)."""
        try:
            stdout, stderr, rc = await run_zfs(
                "get", "-H", "-r", "-o", "name,property,value",
                "compression,recordsize,sync,quota,special_small_blocks,"
                "atime,relatime,canmount,readonly",
                "-t", "filesystem",
                pool_name, check=False, op="read",
            )
            if rc != 0:
                return {}
            result: Dict[str, Dict[str, str]] = {}
            atimes: Dict[str, str] = {}
            relatimes: Dict[str, str] = {}
            for line in stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) != 3:
                    continue
                ds_name, prop, value = parts
                if value == '-':
                    continue
                if prop == 'sync':
                    prop = 'sync_mode'
                if prop == 'atime':
                    atimes[ds_name] = value
                    continue
                if prop == 'relatime':
                    relatimes[ds_name] = value
                    continue
                if ds_name not in result:
                    result[ds_name] = {}
                result[ds_name][prop] = value
            for ds_name, atime in atimes.items():
                result.setdefault(ds_name, {})["atime"] = _params_to_atime(atime, relatimes.get(ds_name))
            return result
        except Exception:
            return {}

    async def create_dataset(
        self,
        db: Session,
        name: str,
        pool_name: str,
        compression: str = "zstd",
        recordsize: str = "128K",
        sync_mode: str = "standard",
        quota: Optional[str] = None,
        special_small_blocks: Optional[str] = None,
        atime: str = "partial",
        canmount: str = "on",
        readonly: str = "off"
    ) -> Dict[str, Any]:
        """Create a new dataset."""
        name = validate_dataset_name(name)

        pool = self.get_pool_by_name(db, pool_name)
        if not pool:
            raise PoolNotFoundError(f"Pool '{pool_name}' not found")

        full_name = f"{pool_name}/{name}"

        if await self.dataset_exists(full_name):
            raise ValidationError(f"Dataset '{full_name}' already exists")

        cmd = [
            "create",
            "-o", f"compression={compression}",
            "-o", f"recordsize={recordsize}",
            "-o", f"sync={sync_mode}",
        ]
        for tok in _atime_to_params(atime):
            cmd.extend(["-o", tok])
        cmd.extend(["-o", f"canmount={canmount}"])
        cmd.extend(["-o", f"readonly={readonly}"])

        if quota:
            cmd.extend(["-o", f"quota={quota}"])

        if special_small_blocks:
            cmd.extend(["-o", f"special_small_blocks={special_small_blocks}"])

        cmd.append(full_name)

        stdout, stderr, returncode = await run_zfs(*cmd, timeout=300, check=False)

        if returncode != 0:
            raise DatasetError(f"Failed to create dataset: {stderr}")

        return {
            "name": full_name,
            "compression": compression,
            "recordsize": recordsize,
            "sync_mode": sync_mode,
            "quota": quota,
            "special_small_blocks": special_small_blocks or "0",
            "atime": atime,
            "canmount": canmount,
            "readonly": readonly,
            "mountpoint": f"/{full_name}",
        }

    async def update_dataset(
        self,
        dataset_name: str,
        compression: Optional[str] = None,
        recordsize: Optional[str] = None,
        sync_mode: Optional[str] = None,
        quota: Optional[str] = None,
        special_small_blocks: Optional[str] = None,
        atime: Optional[str] = None,
        canmount: Optional[str] = None,
        readonly: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update dataset properties via zfs set."""
        validate_dataset_name(dataset_name)

        # Check dataset exists
        if not await self.dataset_exists(dataset_name):
            raise DatasetNotFoundError(f"Dataset '{dataset_name}' not found")

        # Apply property changes via zfs set
        if compression is not None:
            await run_zfs("set", f"compression={compression}", dataset_name)

        if recordsize is not None:
            await run_zfs("set", f"recordsize={recordsize}", dataset_name)

        if sync_mode is not None:
            await run_zfs("set", f"sync={sync_mode}", dataset_name)

        if quota is not None:
            if quota:
                await run_zfs("set", f"quota={quota}", dataset_name)
            else:
                await run_zfs("set", "quota=none", dataset_name)

        if special_small_blocks is not None:
            val = special_small_blocks.strip()
            await run_zfs("set", f"special_small_blocks={val or '0'}", dataset_name)

        if atime is not None:
            for tok in _atime_to_params(atime):
                await run_zfs("set", tok, dataset_name)

        if canmount is not None:
            await run_zfs("set", f"canmount={canmount}", dataset_name)

        if readonly is not None:
            await run_zfs("set", f"readonly={readonly}", dataset_name)

        # Return dataset with live ZFS properties
        live_props = await self.get_dataset_properties(dataset_name)
        return {
            "name": dataset_name,
            **live_props,
        }
