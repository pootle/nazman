from typing import List, Optional, Dict, Any
import json
import re
from datetime import datetime
from sqlalchemy.orm import Session

from ..models.pool import Pool
from ..models.disk import Disk
from ..managers.nfs_manager import nfs_manager
from ..managers.smb_manager import smb_manager
from ..managers.disk_manager import (
    read_slot_uuids, resolve_slot_to_device, resolve_by_id,
    get_device_path, get_device_name,
)
from ..utils.commands import run_zpool, run_zfs, run_command
from ..utils.exceptions import PoolError, DatasetError, ValidationError
from ..utils.validation import validate_pool_name, validate_dataset_name


def _parse_size_bytes(size_str: str) -> float:
    """Parse a ZFS size string (e.g. '3.64T', '1024M', or raw bytes)."""
    if not size_str:
        return 0.0
    s = size_str.strip()
    try:
        return float(s)
    except ValueError:
        pass
    if not s:
        return 0.0
    unit = s[-1].upper()
    multiplier = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    suffixes = ("KB", "MB", "GB", "TB", "PB",
                "KiB", "MiB", "GiB", "TiB", "PiB")
    for suf in suffixes:
        if s.upper().endswith(suf):
            return float(s[:-len(suf)]) * multiplier[suf[0]]
    if unit in multiplier:
        return float(s[:-1]) * multiplier[unit]
    return 0.0


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
    """Manages ZFS pools, datasets, and snapshots."""

    # ── Pool operations ────────────────────────────────────────────────

    async def list_pools(self, db: Session) -> List[Dict[str, Any]]:
        """List all ZFS pools with live status from ZFS."""
        try:
            stdout, stderr, returncode = await run_zpool(
                "list", "-P", "-H", "-o", "name,size,allocated,free,capacity,health",
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

                    pool = db.query(Pool).filter(Pool.name == pool_name).first()
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
                        "type": vtype,
                        "class": vclass,
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
                        target.append({"name": name, "type": vtype, "class": class_key, "children": children, **{k: v for k, v in vdev.items() if k in ("total_space", "state", "alloc_space")}})

            # Determine topology from data vdevs
            if data_vdevs:
                topology = data_vdevs[0].get("type", "stripe") or "stripe"
                if topology == "root":
                    topology = "stripe"

            status_str = pool_info.get("state", "ONLINE").upper()

            return {
                "name": pool_name,
                "status": status_str,
                "topology": topology,
                "vdevs": data_vdevs + special_vdevs + log_vdevs + cache_vdevs,
                "data_vdevs": data_vdevs,
                "special_vdevs": special_vdevs,
                "log_vdevs": log_vdevs,
                "cache_vdevs": cache_vdevs,
                "scan": pool_info.get("scan", {}),
                "config": pool_info.get("config", {})
            }

        except Exception as e:
            if isinstance(e, PoolError):
                raise
            raise PoolError(f"Error getting pool status: {str(e)}")

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
        existing = db.query(Pool).filter(Pool.name == name).first()
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

        role_order = {"special": "special", "log": "log", "cache": "cache"}

        # Vdevs compatible with the single-create path (same ashift or inherit).
        create_groups = {role: [] for role in role_order}
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

        pool = db.query(Pool).filter(Pool.name == pool_name).first()
        if not pool:
            raise PoolError(f"Pool '{pool_name}' not found")

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

    async def get_pool_destroy_info(self, db: Session, pool_name: str) -> Dict[str, Any]:
        """Gather info shown in the pool-destroy confirmation dialog."""
        validate_pool_name(pool_name)

        size_bytes = used_bytes = free_bytes = None
        try:
            stdout, stderr, rc = await run_zpool(
                "list", "-P", "-H", "-o", "name,size,allocated,free", pool_name,
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

        pool = db.query(Pool).filter(Pool.name == pool_name).first()
        export_info = {"exports": [], "active_clients": []}
        if pool is not None:
            export_info = await nfs_manager.get_pool_export_info(db, pool)
        smb_info = {} if pool is None else smb_manager.get_pool_share_info(db, pool)

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
        active_clients = []

        # Enumerate the pool's child datasets (excluding the pool root itself).
        dataset_names = []
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", "-t", "filesystem", "-r", pool_name,
            check=False, op="read",
        )
        if rc == 0:
            for line in stdout.splitlines():
                name = line.strip()
                if name and name != pool_name:
                    dataset_names.append(name)

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
        probe_paths = [f"/{name}" for name in dataset_names]
        if not probe_paths:
            probe_paths = [f"/{pool_name}"]
        try:
            stdout, _, rc = await run_command(
                ["showmount", "-a"], timeout=15, check=False
            )
            if rc == 0:
                for line in stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith("All mount") or ":" not in line:
                        continue
                    client, path = line.rsplit(":", 1)
                    path = path.strip()
                    if any(path == p or path.startswith(p + "/") for p in probe_paths):
                        active_clients.append({
                            "client": client.strip(),
                            "path": path,
                        })
        except Exception:
            pass

        # Active SMB connections on any dataset share in the pool.
        smb_connected = []
        try:
            smb_connected = await self._smb_active_clients(probe_paths)
        except Exception:
            pass

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

        pool = db.query(Pool).filter(Pool.name == pool_name).first()

        # Pre-check: block if any child dataset is busy (mounted/held by an NFS
        # client). `zpool destroy -f` otherwise fails with "cannot unmount".
        obstacles = await self._pool_destroy_obstacles(db, pool_name)
        if obstacles["active_clients"] or obstacles["mounted"] or obstacles.get("smb_connected"):
            parts = []
            if obstacles["mounted"]:
                parts.append(
                    "child dataset(s) still mounted: " + ", ".join(obstacles["mounted"])
                )
            if obstacles["active_clients"]:
                n = len(obstacles["active_clients"])
                parts.append(f"{n} NFS client(s) still connected")
            if obstacles.get("smb_connected"):
                n = len(obstacles["smb_connected"])
                parts.append(f"{n} SMB connection(s) still active on {', '.join(obstacles['smb_connected'])}")
            raise PoolError(
                f'Pool "{pool_name}" has {"; ".join(parts)}. '
                "Unmount these datasets and disconnect NFS/SMB clients before destroying "
                "the pool. (Unmount with e.g. `sudo zfs unmount <dataset>`.)"
            )

        # Unexport any NFS shares and unshare any SMB shares owned by this pool
        if pool is not None:
            try:
                await nfs_manager.unexport_pool(db, pool)
            except Exception:
                pass
            try:
                await smb_manager.unshare_pool(db, pool)
            except Exception:
                pass

        stdout, stderr, returncode = await run_zpool(
            "destroy", "-f", pool_name,
            timeout=300
        )

        if returncode != 0:
            raise PoolError(f"Failed to destroy pool: {stderr}")

        if pool:
            db.delete(pool)
            db.commit()

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
                        batch_props.update(await self._get_all_dataset_properties(ds_pool))

            # Collect pool root dataset names to exclude them
            pool_root_names = {p.name for p in db.query(Pool).all()}

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

    async def _dataset_live_exists(self, dataset_name: str) -> bool:
        """Confirm a dataset currently exists in ZFS by its full name."""
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", dataset_name, check=False, op="read"
        )
        return rc == 0 and dataset_name in stdout.split()

    async def _get_dataset_properties(self, dataset_name: str) -> Dict[str, str]:
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

    async def _get_all_dataset_properties(self, pool_name: str) -> Dict[str, Dict[str, str]]:
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

        pool = db.query(Pool).filter(Pool.name == pool_name).first()
        if not pool:
            raise PoolError(f"Pool '{pool_name}' not found")

        full_name = f"{pool_name}/{name}"

        list_out, _, list_rc = await run_zfs(
            "list", "-H", "-o", "name", full_name, check=False
        )
        if list_rc == 0 and full_name in list_out.split():
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

    async def _dataset_destroy_obstacles(self, db: Session, dataset_name: str) -> Dict[str, Any]:
        """Return obstacles that would prevent destroying a mounted/busy dataset.

        ``mounted`` reflects whether the dataset filesystem is currently mounted,
        ``exports`` lists any defined NFS exports (informational), and
        ``active_clients`` lists NFS clients currently connected to the dataset's
        mount path. Active clients keep the mount busy even after the share is
        removed, which is the common cause of a "cannot unmount" destroy failure.
        """
        mounted = False
        exports = []
        active_clients = []

        try:
            stdout, _, rc = await run_zfs(
                "get", "-H", "-o", "value", "mounted", dataset_name, check=False, op="read",
            )
            if rc == 0 and stdout.strip().lower() == "yes":
                mounted = True
        except Exception:
            pass

        sharenfs = await nfs_manager._read_sharenfs(dataset_name)
        if sharenfs not in ("off", ""):
            exports = [f"/{dataset_name}"]

        mount_path = f"/{dataset_name}"
        try:
            stdout, _, rc = await run_command(
                ["showmount", "-a"], timeout=15, check=False
            )
            if rc == 0:
                for line in stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith("All mount") or ":" not in line:
                        continue
                    client, path = line.rsplit(":", 1)
                    path = path.strip()
                    if path == mount_path or path.startswith(mount_path + "/"):
                        active_clients.append({
                            "client": client.strip(),
                            "path": path,
                        })
        except Exception:
            pass  # showmount unavailable or no NFS server

        return {
            "mounted": mounted,
            "exports": exports,
            "active_clients": active_clients,
            "smb_share": smb_manager.list_shares(db),
        }

    async def destroy_dataset(self, db: Session, dataset_name: str, recursive: bool = False) -> None:
        """Destroy a dataset (DESTRUCTIVE)."""
        obstacles = await self._dataset_destroy_obstacles(db, dataset_name)
        smb_share = next((s for s in (obstacles.get("smb_share") or [])
                          if s["dataset_name"] == dataset_name), None)
        if obstacles["mounted"] or obstacles["active_clients"] or smb_share:
            parts = []
            if obstacles["mounted"]:
                parts.append("still mounted")
            if obstacles["active_clients"]:
                n = len(obstacles["active_clients"])
                parts.append(f"{n} NFS client(s) still connected")
            if smb_share:
                parts.append("has an active SMB share")
            raise DatasetError(
                f'Dataset "{dataset_name}" is {", ".join(parts)}. '
                "Remove the SMB share, unmount the dataset, and disconnect NFS clients "
                "before destroying."
            )

        cmd = ["destroy"]
        if recursive:
            cmd.append("-r")
        cmd.append(dataset_name)

        stdout, stderr, returncode = await run_zfs(*cmd, timeout=300, check=False)

        if returncode != 0:
            raise DatasetError(f"Failed to destroy dataset: {stderr}")

    # ── Snapshot operations ────────────────────────────────────────────

    async def create_snapshot(
        self,
        db: Session,
        dataset_name: str,
        snapshot_name: str
    ) -> Dict[str, Any]:
        """Create a snapshot of a dataset (no DB persistence)."""
        list_out, _, list_rc = await run_zfs(
            "list", "-H", "-o", "name", dataset_name, check=False, op="read"
        )
        if list_rc != 0 or dataset_name not in list_out.split():
            raise DatasetError(f"Dataset '{dataset_name}' not found")

        full_snapshot_name = f"{dataset_name}@{snapshot_name}"

        stdout, stderr, returncode = await run_zfs(
            "snapshot", full_snapshot_name,
            timeout=300, check=False
        )

        if returncode != 0:
            raise DatasetError(f"Failed to create snapshot: {stderr}")

        return {
            "name": full_snapshot_name,
            "dataset_name": dataset_name,
            "snapshot_name": snapshot_name,
        }

    async def list_snapshots(self, db: Session, dataset_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List snapshots from ZFS (no DB)."""
        try:
            cmd = ["list", "-H", "-o", "name,used,referenced,creation", "-t", "snapshot"]
            if dataset_name:
                cmd.extend(["-r", dataset_name])

            stdout, stderr, returncode = await run_zfs(*cmd, check=False, op="read")

            if returncode != 0:
                raise DatasetError(f"Failed to list snapshots: {stderr}")

            snapshots = []
            for line in stdout.strip().split('\n'):
                if not line:
                    continue

                parts = line.split('\t')
                if len(parts) >= 3:
                    full_name = parts[0]
                    if '@' in full_name:
                        ds_name, snap_name = full_name.split('@', 1)
                        snapshots.append({
                            "name": full_name,
                            "dataset_name": ds_name,
                            "snapshot_name": snap_name,
                            "used": parts[1] if parts[1] != '-' else None,
                            "referenced": parts[2] if parts[2] != '-' else None,
                            "creation": parts[3] if len(parts) > 3 and parts[3] != '-' else None,
                        })

            return snapshots

        except Exception as e:
            if isinstance(e, DatasetError):
                raise
            raise DatasetError(f"Error listing snapshots: {str(e)}")

    async def destroy_snapshot(self, db: Session, snapshot_name: str) -> None:
        """Destroy a snapshot (DESTRUCTIVE). No DB record to remove."""
        stdout, stderr, returncode = await run_zfs(
            "destroy", snapshot_name,
            timeout=300, check=False
        )

        if returncode != 0:
            raise DatasetError(f"Failed to destroy snapshot: {stderr}")


zfs_manager = ZfsManager()