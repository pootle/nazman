from typing import List, Dict, Any, Tuple
import asyncio
import json
import uuid
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from ..models.disk import Disk
from ..utils.commands import run_command
from ..utils.devices import (
    resolve_by_id, get_device_path, get_device_name,
    read_slot_uuids, write_slot_uuid,
    os_disk_names, refresh_device_map, MMC_SUBDEVICE_RE,
)
from ..utils.sizes import parse_size_to_bytes
from ..utils.exceptions import DiskError, DiskNotFoundError
from ..utils.validation import validate_device_path
from ..utils.zfs_query import pool_member_for_disk
from sqlalchemy.exc import OperationalError, DatabaseError

# SMART raw-value counters that indicate physical problems when non-zero.
_SMART_RAW_PROBLEM_ATTRS = {
    "Reallocated_Sector_Ct",
    "Reallocated_Event_Count",
    "Current_Pending_Sector",
    "Offline_Uncorrectable",
    "Reported_Uncorrect",
    "UDMA_CRC_Error_Count",
}


class DiskManager:
    """Manages disk discovery, health and partition lifecycle.

    Stable device identity (by-id paths, kernel-name map, slot UUIDs) lives in
    :mod:`nazman.utils.devices`; this class owns the disk *domain*: discovery,
    DB sync, SMART reporting and partition/wipe operations.

    Pool membership checks need ZFS knowledge without importing ZfsManager
    (which would create a cycle), so ``pool_members_provider`` is injected at
    wiring time: an async callable returning the ``{device path: pool name}``
    map (``ZfsManager.get_pool_members``).
    """

    def __init__(self, pool_members_provider=None) -> None:
        self._pool_members_provider = pool_members_provider

    def set_pool_members_provider(self, provider) -> None:
        """Inject the async callable returning {device path -> pool name}."""
        self._pool_members_provider = provider

    async def get_pool_members(self) -> Dict[str, str]:
        if self._pool_members_provider is None:
            return {}
        return await self._pool_members_provider()

    async def discover_disks(self) -> List[Dict[str, Any]]:
        """Discover all block devices on the system."""
        try:
            os_names = await os_disk_names()

            stdout, stderr, returncode = await run_command(
                ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN"],
                timeout=30, op="read", category="disk",
            )

            if returncode != 0:
                raise DiskError(f"Failed to discover disks: {stderr}")

            data = json.loads(stdout)
            disks = []

            for device in data.get("blockdevices", []):
                if device.get("type") != "disk":
                    continue

                device_name = device.get("name", "")
                if MMC_SUBDEVICE_RE.match(device_name):
                    continue

                disk_info = {
                    "device_name": device_name,
                    "device_path": f"/dev/{device_name}",
                    "by_id": resolve_by_id(device_name),
                    "model": device.get("model", "").strip() if device.get("model") else None,
                    "serial": device.get("serial", "").strip() if device.get("serial") else None,
                    "size_bytes": parse_size_to_bytes(device.get("size", "0")),
                    "disk_type": self._determine_disk_type(device),
                    "rotation_speed": device.get("rota"),
                    "is_os_disk": device_name in os_names,
                }
                disks.append(disk_info)

            return disks

        except Exception as e:
            if isinstance(e, DiskError):
                raise
            raise DiskError(f"Error discovering disks: {str(e)}")

    async def _read_smartctl(self, device_path: str) -> Dict[str, Any]:
        """Run smartctl in JSON mode and return the parsed report."""
        validate_device_path(device_path)
        stdout, _, _ = await run_command(
            ["smartctl", "-a", "-j", device_path],
            timeout=30,
            check=False,
            op="read",
            category="smartctl",
        )
        data = json.loads(stdout) if stdout else {}
        if not data:
            raise DiskError(f"No SMART data returned for {device_path}")
        return data

    async def get_disk_health(self, device_path: str) -> Dict[str, Any]:
        """Get overall disk health information using smartctl."""
        try:
            data = await self._read_smartctl(device_path)
        except DiskError:
            raise
        except Exception:
            return {"temperature": None, "power_on_hours": None, "health_status": "unknown"}

        temperature = None
        power_on_hours = None
        for attr in data.get("ata_smart_attributes", {}).get("table", []):
            if attr.get("name") == "Temperature_Celsius" and temperature is None:
                temperature = attr.get("value")
            elif attr.get("name") == "Power_On_Hours" and power_on_hours is None:
                power_on_hours = attr.get("value")

        smart_status = data.get("smart_status")
        if smart_status and smart_status.get("passed") is False:
            health_status = "failing"
        elif smart_status and smart_status.get("passed") is True:
            health_status = "ok"
        else:
            health_status = "unknown"

        return {
            "temperature": temperature,
            "power_on_hours": power_on_hours,
            "health_status": health_status,
        }

    async def get_smart_details(self, device_path: str) -> Dict[str, Any]:
        """Full SMART report for the disk details view.

        Combines the overall self-assessment, every attribute (with threshold
        status), a curated list of human-readable problems, and the self-test
        log.  Returns a stub with a populated ``errors`` list when smartctl
        reports nothing usable.
        """
        try:
            data = await self._read_smartctl(device_path)
        except Exception:
            return {
                "model_name": None,
                "health_status": "unknown",
                "passed": None,
                "temperature": None,
                "power_on_hours": None,
                "problems": ["SMART data unavailable"],
                "attributes": [],
                "self_test": [],
                "nvme": None,
            }

        def _int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        attributes = []
        problems = []
        temperature = None
        power_on_hours = None
        for attr in data.get("ata_smart_attributes", {}).get("table", []):
            name = attr.get("name") or ""
            when_failed = (attr.get("when_failed") or "").strip()
            raw = _int((attr.get("raw") or {}).get("value"))
            attributes.append({
                "id": attr.get("id"),
                "name": name,
                "value": attr.get("value"),
                "worst": attr.get("worst"),
                "thresh": attr.get("thresh"),
                "when_failed": when_failed,
                "flags": (attr.get("flags") or {}).get("string", "").strip(),
                "raw": raw,
            })
            if name == "Temperature_Celsius" and temperature is None:
                temperature = attr.get("value")
            elif name == "Power_On_Hours" and power_on_hours is None:
                power_on_hours = attr.get("value")
            if when_failed:
                problems.append(
                    f"{name} below threshold (value {attr.get('value')}"
                    f", worst {attr.get('worst')}, thresh {attr.get('thresh')})"
                )
            elif name in _SMART_RAW_PROBLEM_ATTRS and raw:
                problems.append(f"{name}: {raw}")

        smart_status = data.get("smart_status") or {}
        passed = smart_status.get("passed")
        if passed is False:
            health_status = "failing"
            problems.insert(0, "SMART overall-health self-assessment FAILED")
        elif passed is True:
            health_status = "ok"
        else:
            health_status = "unknown"

        nvme_data = data.get("nvme_smart_health_information_log")
        nvme = None
        if isinstance(nvme_data, dict) and nvme_data:
            critical_warning = _int(nvme_data.get("critical_warning"))
            percentage_used = _int(nvme_data.get("percentage_used"))
            media_errors = _int(nvme_data.get("media_errors"))
            nvme = {
                "critical_warning": critical_warning,
                "percentage_used": percentage_used,
                "media_errors": media_errors,
            }
            if critical_warning:
                problems.append(f"NVMe critical warning: 0x{critical_warning:02x}")
            if media_errors:
                problems.append(f"NVMe media errors: {media_errors}")
            if percentage_used is not None and percentage_used >= 90:
                problems.append(f"NVMe percentage used: {percentage_used}%")

        problems = list(dict.fromkeys(problems))

        self_test = []
        for entry in data.get("ata_smart_self_test_log", {}).get("table", []):
            status = entry.get("status") or ""
            if not status:
                continue
            self_test.append({
                "type": entry.get("type"),
                "status": entry.get("status"),
                "remaining": entry.get("remaining"),
                "lifetime_hours": entry.get("lifetime_hours"),
                "failed": "without error" not in status.lower(),
            })

        return {
            "model_name": data.get("model_name"),
            "model_family": data.get("model_family"),
            "device_type": data.get("device", {}).get("type"),
            "health_status": health_status,
            "passed": passed,
            "temperature": temperature,
            "power_on_hours": power_on_hours,
            "problems": problems,
            "attributes": attributes,
            "self_test": self_test,
            "nvme": nvme,
        }

    async def sync_disks_to_database(self, db: Session) -> List[Disk]:
        """Sync discovered disks to database.

        Reads (discovery + SMART health) are done outside any write lock.
        The DB write phase is retried on SQLite ``database is locked`` errors
        caused by concurrent writers (e.g. APScheduler background jobs).

        ``device_name``/``device_path`` (ephemeral kernel names) are refreshed
        in the in-memory device map and are NOT stored in the database.
        """
        discovered = await self.discover_disks()

        refresh_device_map(discovered)

        for disk_info in discovered:
            disk_info["_health"] = await self.get_disk_health(disk_info["device_path"])

        for attempt in range(5):
            try:
                self._sync_write(db, discovered)
                break
            except (OperationalError, DatabaseError) as exc:
                if "locked" not in str(exc).lower():
                    raise
                if attempt == 4:
                    raise
                db.rollback()
                await asyncio.sleep(0.25 * (attempt + 1))

        return db.query(Disk).all()

    def _sync_write(self, db: Session, discovered: list) -> None:
        """Perform the actual DB mutations for sync. Called in a retry loop.

        Identity is keyed strictly by ``by_id`` then ``serial``.  Drives that
        are no longer present in this scan are retained and marked ``removed``
        (so knowledge of replaced disks is preserved) rather than deleted.
        """
        seen_ids = set()

        for disk_info in discovered:
            by_id = disk_info.get("by_id")
            serial = disk_info.get("serial")

            existing = None
            if by_id:
                existing = db.query(Disk).filter(Disk.by_id == by_id).first()
            if not existing and serial:
                existing = db.query(Disk).filter(Disk.serial == serial).first()
                if existing and not existing.by_id:
                    existing.by_id = by_id

            if existing:
                if existing.id is not None:
                    seen_ids.add(existing.id)
                existing.model = disk_info.get("model")
                existing.serial = disk_info.get("serial")
                existing.size_bytes = disk_info["size_bytes"]
                existing.disk_type = disk_info["disk_type"]
                existing.rotation_speed = disk_info.get("rotation_speed")
                existing.is_os_disk = disk_info.get("is_os_disk", False)
                health = disk_info.get("_health", {})
                existing.temperature = health.get("temperature")
                existing.power_on_hours = health.get("power_on_hours")
                existing.health_status = health.get("health_status")
                if existing.status == "removed":
                    existing.status = "active"
                existing.updated_at = datetime.now(timezone.utc)
            else:
                if not by_id:
                    continue
                disk = Disk(
                    by_id=by_id,
                    model=disk_info.get("model"),
                    serial=disk_info.get("serial"),
                    size_bytes=disk_info["size_bytes"],
                    disk_type=disk_info["disk_type"],
                    rotation_speed=disk_info.get("rotation_speed"),
                    is_os_disk=disk_info.get("is_os_disk", False),
                    temperature=disk_info.get("_health", {}).get("temperature"),
                    power_on_hours=disk_info.get("_health", {}).get("power_on_hours"),
                    health_status=disk_info.get("_health", {}).get("health_status")
                )
                db.add(disk)
                db.flush()
                if disk.id is not None:
                    seen_ids.add(disk.id)

        # Retain rows for disks that vanished in this scan; mark them removed.
        if seen_ids:
            db.query(Disk).filter(
                ~Disk.id.in_(list(seen_ids))
            ).update({"status": "removed"}, synchronize_session=False)

        db.commit()

    async def secure_wipe_disk(self, db: Session, disk_id: int) -> Dict[str, Any]:
        """Securely wipe a disk using the appropriate method for its media type."""
        disk = await self._assert_writable(db, disk_id)
        device_path = resolve_by_id(disk.by_id) if disk.by_id else None
        if not device_path:
            raise DiskError(f"Disk {get_device_name(disk) or disk.serial or disk.id} is not currently present")
        method = None

        if disk.disk_type == "nvme":
            method = "nvme_format"
            await run_command(
                ["nvme", "format", "-s1", device_path],
                timeout=120
            )
        elif disk.disk_type == "ssd":
            method = "blkdiscard"
            await run_command(
                ["blkdiscard", device_path],
                timeout=120
            )
        else:
            method = "dd_urandom"
            await run_command(
                ["dd", "if=/dev/urandom", f"of={device_path}", "bs=1M", "status=progress"],
                timeout=3600,
                check=False
            )

        return {
            "disk": get_device_name(disk) or disk.serial or disk.model,
            "method": method,
            "success": True
        }

    def _determine_disk_type(self, device: Dict[str, Any]) -> str:
        """Determine disk type from device information."""
        name = (device.get("name") or "").lower()
        model = (device.get("model") or "").lower()

        if "nvme" in name or "nvme" in model:
            return "nvme"
        if device.get("rota") == 0 or "ssd" in model:
            return "ssd"
        return "hdd"

    def live_device_path(self, disk: Disk, action: str = "this operation") -> str:
        """Resolve the current ephemeral kernel path for a disk, or raise DiskError."""
        path = get_device_path(disk)
        if not path:
            raise DiskError(
                f"Disk {get_device_name(disk) or disk.serial or disk.id} is not currently present; cannot {action}"
            )
        return path

    def get_disk(self, db: Session, disk_id: int) -> Disk:
        """Return the Disk row or raise DiskNotFoundError."""
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise DiskNotFoundError(f"Disk with id {disk_id} not found")
        return disk

    async def _assert_writable(self, db: Session, disk_id: int) -> Disk:
        """Validate a disk can be wiped/re-partitioned: exists, not the OS disk, not a pool member.

        Returns the ``Disk`` row.  Pool membership is resolved from a single
        ``zpool status`` call via the injected pool-members provider, so batch
        operations share one lookup instead of one subprocess per disk.
        """
        disk = self.get_disk(db, disk_id)
        if disk.is_os_disk:
            raise DiskError("Cannot modify the OS disk")
        pool_members = await self.get_pool_members()
        pool_name = pool_member_for_disk(pool_members, disk)
        if pool_name:
            raise DiskError(f"Disk is a member of pool '{pool_name}'; remove it from the pool first")
        return disk

    async def _reset_partition_table(self, device_path: str) -> None:
        """Wipe all signatures and create a fresh empty GPT partition table."""
        await run_command(["wipefs", "-a", device_path], timeout=60)
        await run_command(["parted", "-s", device_path, "mklabel", "gpt"], timeout=60)

    async def get_disk_usage(self, disks: List[Disk]) -> Dict[int, Dict[str, Any]]:
        """Partition count and free (unpartitioned) space % for the given disks.

        Runs a single ``lsblk`` read for all currently-present disks.  Returns
        ``{disk_id: {"partition_count": int, "free_percent": int|None}}``.
        ``free_percent`` is None when the disk is not present (no live
        partition information available).
        """
        present: List[Tuple[Disk, str]] = []
        for disk in disks:
            path = get_device_path(disk)
            if path:
                present.append((disk, path))
        if not present:
            return {}
        slot_info = await read_slot_uuids([path for _, path in present])
        usage: Dict[int, Dict[str, Any]] = {}
        for disk, path in present:
            parts = slot_info.get(path, {}).get("partitions", [])
            used = sum(p.get("size_bytes", 0) for p in parts)
            free_bytes = max(disk.size_bytes - used, 0)
            usage[disk.id] = {
                "partition_count": len(parts),
                "free_percent": round(free_bytes * 100 / disk.size_bytes) if disk.size_bytes else 0,
            }
        return usage

    async def wipe_disk(self, db: Session, disk_id: int) -> Dict[str, Any]:
        """Wipe all partition tables from a disk."""
        disk = await self._assert_writable(db, disk_id)
        device_path = self.live_device_path(disk, action="wipe")
        await self._reset_partition_table(device_path)
        return {"message": f"Wiped partition table from {get_device_name(disk) or disk.model or disk.serial}"}

    async def partition_disk(
        self, db: Session, disk_id: int, partitions_spec: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Partition a disk. Generates slot UUIDs and writes them to GPT names."""
        disk = await self._assert_writable(db, disk_id)
        device_path = self.live_device_path(disk, action="partition")

        await self._reset_partition_table(device_path)

        partition_number = 1
        current_sector = 2048

        for spec in partitions_spec:
            size_mb = spec.get("size_mb")

            start_sector = current_sector
            if size_mb:
                end_sector = start_sector + (size_mb * 2048)
            else:
                end_sector = -1

            if end_sector == -1:
                await run_command([
                    "parted", "-s", device_path, "mkpart", "primary",
                    f"{start_sector}s", "100%"
                ], timeout=60)
            else:
                await run_command([
                    "parted", "-s", device_path, "mkpart", "primary",
                    f"{start_sector}s", f"{end_sector}s"
                ], timeout=60)

            slot_uuid = str(uuid.uuid4())
            await write_slot_uuid(device_path, partition_number, slot_uuid)

            if end_sector != -1:
                current_sector = end_sector + 1

            partition_number += 1

        return {"disk_id": disk.id, "device_name": get_device_name(disk) or disk.model or disk.serial, "success": True}

    async def batch_wipe_disks(self, db: Session, disk_ids: List[int]) -> List[Dict[str, Any]]:
        """Wipe partition tables from multiple disks (no new partitions created)."""
        results = []
        for disk_id in disk_ids:
            try:
                disk = await self._assert_writable(db, disk_id)
                device_path = self.live_device_path(disk, action="wipe")
                await self._reset_partition_table(device_path)
                results.append({"disk_id": disk.id, "device_name": get_device_name(disk) or disk.model or disk.serial, "success": True})
            except Exception as e:
                results.append({"disk_id": disk_id, "success": False, "error": str(e)})
        return results

    async def update_disk(self, db: Session, disk_id: int, updates: Dict[str, Any]) -> Disk:
        """Update disk fields (status, etc.)."""
        disk = self.get_disk(db, disk_id)

        allowed = {"status"}
        for key, value in updates.items():
            if key in allowed:
                setattr(disk, key, value)

        db.commit()
        db.refresh(disk)
        return disk

    async def drop_disk(self, db: Session, disk_id: int) -> Dict[str, Any]:
        """Permanently remove a disk row that is no longer present."""
        disk = self.get_disk(db, disk_id)

        if get_device_path(disk):
            raise DiskError(
                f"Disk {get_device_name(disk) or disk.serial or disk.id} is currently present; cannot drop it"
            )

        label = get_device_name(disk) or disk.serial or disk.by_id or disk.id
        db.query(Disk).filter(Disk.id == disk_id).delete()
        db.commit()
        return {"message": f"Dropped disk record for {label}"}

    async def resurrect_disk(self, db: Session, disk_id: int) -> Dict[str, Any]:
        """Reactivate a dead disk."""
        disk = self.get_disk(db, disk_id)
        if disk.status != "dead":
            raise DiskError("Disk is not dead")
        disk.status = "active"
        db.commit()
        db.refresh(disk)
        return {"message": f"Disk {get_device_name(disk) or disk.model or disk.serial} resurrected"}

