from typing import List, Optional, Dict, Any, Tuple
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from ..models.disk import Disk
from ..utils.commands import run_command, run_command_sync
from ..utils.exceptions import DiskError, ValidationError
from ..utils.validation import validate_device_path
from sqlalchemy.exc import OperationalError, DatabaseError

# In-memory registry of ephemeral kernel names, keyed by stable identity.
# device_name/device_path are NOT persisted: /dev/sdX changes on boot and
# hot-plug, so we rebuild this map (from lsblk + by-id resolution) whenever the
# service starts or a scan runs.  Commands that need a live device path look it
# up here rather than in the database.
_device_map: Dict[str, Dict[str, Any]] = {}  # by_id -> {kernel_name, device_path}
_serial_device_map: Dict[str, Dict[str, Any]] = {}  # serial -> {kernel_name, device_path}


def refresh_device_map(discovered: List[Dict[str, Any]]) -> None:
    """Rebuild the in-memory by_id/serial -> kernel path map from discovery.

    ``discovered`` entries are the transient dicts produced by
    :func:`DiskManager.discover_disks` (which include ``device_name`` and
    ``device_path``).  Only entries with a stable identity are retained.
    """
    global _device_map, _serial_device_map
    _device_map = {}
    _serial_device_map = {}
    for info in discovered:
        by_id = info.get("by_id")
        serial = info.get("serial")
        if not by_id and not serial:
            continue
        entry = {
            "device_name": info.get("device_name"),
            "device_path": info.get("device_path"),
        }
        if by_id:
            _device_map[by_id] = entry
        if serial:
            _serial_device_map[serial] = entry


def clear_device_map() -> None:
    """Drop all in-memory kernel-name knowledge (e.g. on shutdown)."""
    global _device_map, _serial_device_map
    _device_map = {}
    _serial_device_map = {}

def get_device_entry(disk: Disk) -> Optional[Dict[str, Optional[str]]]:
    """Return the current transient kernel name/path for a disk, or None if the
    disk is not currently present in the system."""
    if disk.by_id and disk.by_id in _device_map:
        return _device_map[disk.by_id]
    if disk.serial and disk.serial in _serial_device_map:
        return _serial_device_map[disk.serial]
    return None

def get_device_path(disk) -> Optional[str]:
    entry = get_device_entry(disk)
    return entry["device_path"] if entry else None


def get_device_name(disk) -> Optional[str]:
    entry = get_device_entry(disk)
    return entry["device_name"] if entry else None


async def get_os_reserved_partition_names() -> set:
    """Module-level helper returning OS-reserved partition kernel names."""
    return await DiskManager()._get_os_reserved_partitions()


BY_ID_DIR = "/dev/disk/by-id"

# Preferred by-id prefixes, in order. Model+serial based IDs (ata/nvme/mmc) are
# the most stable and human-readable; wwn/scsi are fallbacks.
_SAFE_PREFIXES = ["ata-", "nvme-", "mmc-", "scsi-", "wwn-", "dm-"]

# eMMC boot/RPMB hardware sub-devices (mmcblk0boot0, mmcblk0boot1, mmcblk0rpmb).
# They inherit the parent eMMC's serial/by-id and cannot be uniquely identified,
# so they are excluded from disk discovery.
_MMC_SUBDEVICE_RE = re.compile(r"^mmcblk[0-9]+(?:boot[0-9]+|rpmb)$")


def resolve_by_id(device_name: str) -> Optional[str]:
    """Resolve the canonical /dev/disk/by-id path for a kernel device name.

    ``device_name`` is e.g. "sda" or "nvme0n1". Returns "/dev/disk/by-id/ata-..."
    or None if the device has no by-id symlink (e.g. a loop device).
    """
    target = f"../../{device_name}"
    if not os.path.isdir(BY_ID_DIR):
        return None
    candidates = []
    try:
        for name in os.listdir(BY_ID_DIR):
            link = os.path.join(BY_ID_DIR, name)
            try:
                if os.readlink(link) == target:
                    candidates.append(name)
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    if not candidates:
        return None

    def prio(name: str) -> int:
        for i, prefix in enumerate(_SAFE_PREFIXES):
            if name.startswith(prefix):
                return i
        return len(_SAFE_PREFIXES)

    candidates.sort(key=prio)
    return f"{BY_ID_DIR}/{candidates[0]}"


def partition_by_id(disk_by_id: Optional[str], partition_number: int) -> Optional[str]:
    """Build the by-id path for a partition from its disk's by-id path.

    '/dev/disk/by-id/ata-ST3000...-part1' style. Returns None if disk_by_id
    is None.
    """
    if not disk_by_id or partition_number <= 0:
        return None
    return f"{disk_by_id}-part{partition_number}"


async def write_slot_uuid(device_path: str, partition_number: int, slot_uuid: str) -> None:
    """Write a GPT partition name (PARTLABEL) containing a slot UUID.

    The label is written as ``nazman:{slot_uuid}`` so it can be read back
    later by :func:`read_slot_uuids`.  Tries sfdisk, then sgdisk.
    """
    label = f"nazman:{slot_uuid}"
    try:
        await run_command(
            ["sfdisk", "--part-label", device_path, str(partition_number), label],
            timeout=30,
            op="write",
            category="disk",
        )
        return
    except Exception:
        pass
    # Fallback: sgdisk
    await run_command(
        ["sgdisk", "--change-name", f"{partition_number}:{label}", device_path],
        timeout=30,
        op="write",
        category="disk",
    )


async def read_slot_uuids(disk_paths: List[str]) -> Dict[str, Dict[str, Any]]:
    """Read GPT partition names for the given disks in a single ``lsblk`` call.

    Returns a dict mapping each disk's device path to its partitions::

        {
            "/dev/sda": {
                "partitions": [
                    {"name": "sda1", "partlabel": "nazman:uuid-1", "slot_uuid": "uuid-1", "size_bytes": 123},
                    ...
                ]
            }
        }

    Uses ``nazman:`` prefix from PARTLABEL as slot UUID.  Falls back to
    the partition's PARTUUID when no nazman label is found.
    """
    if not disk_paths:
        return {}

    stdout, _, rc = await run_command(
        ["lsblk", "-J", "-o", "NAME,TYPE,PARTLABEL,PARTUUID,SIZE"],
        timeout=30,
        op="read",
        category="disk",
    )
    if rc != 0:
        return {}

    data = json.loads(stdout)
    result: Dict[str, Dict[str, Any]] = {}

    def _base_name(path: str) -> str:
        """Strip /dev/ prefix to get kernel name like 'sda'."""
        return path.removeprefix("/dev/") if path.startswith("/dev/") else path

    # Build lookup: base_name -> all requested paths (could be /dev/sda or /dev/disk/by-id/...)
    requested_base = {}
    for p in disk_paths:
        requested_base[_base_name(p)] = p

    def walk(devices, parent_key=None):
        for dev in devices:
            dev_type = dev.get("type", "")
            dev_name = dev.get("name", "")
            dev_base = _base_name(dev_name)

            if dev_type == "disk" and dev_base in requested_base:
                orig_path = requested_base[dev_base]
                result[orig_path] = {"partitions": []}
                walk(dev.get("children", []), orig_path)
            elif dev_type == "part" and parent_key and parent_key in result:
                partlabel = dev.get("partlabel") or ""
                slot_uuid = None
                if partlabel.startswith("nazman:"):
                    slot_uuid = partlabel[len("nazman:"):]
                else:
                    # Fallback: use the partition's PARTUUID — always present for
                    # GPT parts even when there is no filesystem (UUID would be null)
                    slot_uuid = dev.get("partuuid") or None

                size_str = dev.get("size", "0")
                size_bytes = 0
                try:
                    if isinstance(size_str, (int, float)):
                        size_bytes = int(size_str)
                    elif isinstance(size_str, str):
                        multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
                        s = size_str.strip()
                        if s and s[-1] in multipliers:
                            size_bytes = int(float(s[:-1]) * multipliers[s[-1]])
                        elif s:
                            size_bytes = int(float(s))
                except (ValueError, IndexError):
                    size_bytes = 0

                result[parent_key]["partitions"].append({
                    "name": dev_name,
                    "partlabel": partlabel or None,
                    "slot_uuid": slot_uuid,
                    "size_bytes": size_bytes,
                })

                if dev.get("children"):
                    walk(dev.get("children", []), parent_key)

    walk(data.get("blockdevices", []))
    return result


def resolve_slot_to_device(
    disk_by_id: Optional[str],
    slot_uuid: str,
    partitions: List[Dict[str, Any]],
) -> Optional[str]:
    """Resolve a slot UUID to a partition device by-id path.

    ``partitions`` is the list from :func:`read_slot_uuids` for the disk
    containing this partition.  Returns the by-id path (e.g.
    ``/dev/disk/by-id/ata-X-part2``) or None if not found.
    """
    if not disk_by_id:
        return None
    for part in partitions:
        if part.get("slot_uuid") == slot_uuid:
            part_name = part["name"]
            m = re.search(r'(\d+)$', part_name)
            if not m:
                continue
            part_num = int(m.group(1))
            if part_num:
                return partition_by_id(disk_by_id, part_num)
    return None


class DiskManager:
    """Manages disk discovery and health."""

    async def _get_os_disk_names(self) -> set:
        """Detect the physical disks the OS is running on.

        Resolves every physical disk underneath the root filesystem, correctly
        handling software RAID where an md array spans multiple physical disks
        (each member disk is identified, not just one).
        """
        os_disks = set()
        try:
            reserved = await self._get_os_reserved_partitions()
        except Exception:
            return os_disks
        for part_name in reserved:
            disk = self._physical_disk_for_partition(part_name)
            if disk:
                os_disks.add(disk)
        return os_disks

    async def _get_os_reserved_partitions(self) -> set:
        """Return the set of partition kernel names reserved for the OS.

        This includes every partition that is part of the root filesystem's
        backing device (e.g. members of a root md RAID array) plus the boot/EFI
        partition.  Free data partitions on the same physical disk are NOT
        included, so they may still be offered for pool creation.
        """
        names = set()
        for mnt in ("/", "/boot", "/boot/efi"):
            stdout, _, rc = await run_command(
                ["findmnt", "-n", "-o", "SOURCE", mnt], timeout=10, check=False,
                op="read", category="disk",
            )
            if rc == 0 and stdout.strip():
                names.update(await self._resolve_backing_partitions(stdout.strip()))
        return names

    def _physical_disk_for_partition(self, part_name: str) -> Optional[str]:
        """Return the physical disk (e.g. ``nvme0n1``) that owns ``part_name``."""
        if not part_name or part_name.startswith(("md", "loop")):
            return None
        if part_name.startswith(("nvme", "mmcblk")):
            idx = part_name.rfind("p")
            return part_name[:idx] if idx != -1 and part_name[idx + 1:].isdigit() else None
        # sda1 -> sda, vda1 -> vda, xvda1 -> xvda
        base = part_name.rstrip("0123456789")
        return base or None

    async def _resolve_backing_partitions(self, source: str) -> set:
        """Resolve the partition names backing a mount source.

        ``source`` is e.g. ``/dev/md0p1`` or ``/dev/nvme0n1p2``.  If it is a
        partition of an md array, returns all of the array's member partitions
        across all physical disks.  Otherwise returns the partition itself.
        """
        names = set()
        if not source:
            return names

        src_name = source.split("/")[-1]

        backing = None
        if src_name.startswith("md") and "p" in src_name:
            backing = src_name.split("p")[0]

        if not backing:
            names.add(src_name)
            return names

        # Collect the physical member partitions of the md array.  In the
        # member map (parent -> children), the array appears as a child of each
        # physical partition that carries it, e.g. nvme0n1p2 -> md0.
        member_map = await self._get_lsblk_member_map()
        members = {
            parent for parent, children in member_map.items()
            if backing in children
        }
        if members:
            names.update(members)
        else:
            names.add(src_name)
        return names

    async def _get_lsblk_member_map(self) -> Dict[str, set]:
        """Build {name: {child_names}} from ``lsblk -o NAME,TYPE,PKNAME``.

        Uses a multi-parent map so an md array spanning multiple disks maps
        every member partition, rather than only a single parent.
        """
        member_map: Dict[str, set] = {}
        try:
            stdout, _, rc = await run_command(
                ["lsblk", "-J", "-o", "NAME,TYPE,PKNAME"], timeout=10, check=False,
                op="read", category="disk",
            )
            if rc != 0 or not stdout.strip():
                return member_map
            data = json.loads(stdout)

            def walk(devices):
                for dev in devices:
                    name = dev.get("name", "")
                    pkname = dev.get("pkname")
                    if name and pkname:
                        member_map.setdefault(pkname, set()).add(name)
                    walk(dev.get("children", []))

            walk(data.get("blockdevices", []))
        except Exception:
            return {}
        return member_map

    async def discover_disks(self) -> List[Dict[str, Any]]:
        """Discover all block devices on the system."""
        try:
            os_disk_names = await self._get_os_disk_names()

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
                if _MMC_SUBDEVICE_RE.match(device_name):
                    continue

                disk_info = {
                    "device_name": device_name,
                    "device_path": f"/dev/{device_name}",
                    "by_id": resolve_by_id(device_name),
                    "model": device.get("model", "").strip() if device.get("model") else None,
                    "serial": device.get("serial", "").strip() if device.get("serial") else None,
                    "size_bytes": self._parse_size(device.get("size", "0")),
                    "disk_type": self._determine_disk_type(device),
                    "rotation_speed": device.get("rota"),
                    "is_os_disk": device_name in os_disk_names,
                }
                disks.append(disk_info)

            return disks

        except Exception as e:
            if isinstance(e, DiskError):
                raise
            raise DiskError(f"Error discovering disks: {str(e)}")

    async def get_disk_health(self, device_path: str) -> Dict[str, Any]:
        """Get disk health information using smartctl."""
        validate_device_path(device_path)

        try:
            stdout, stderr, returncode = await run_command(
                ["smartctl", "-a", "-j", device_path],
                timeout=30,
                check=False,
                op="read",
                category="smartctl",
            )

            data = json.loads(stdout) if stdout else {}

            if not data:
                raise DiskError(f"No SMART data returned for {device_path}")

            health_info = {
                "temperature": None,
                "power_on_hours": None,
                "health_status": "unknown"
            }

            for attr in data.get("ata_smart_attributes", {}).get("table", []):
                if attr.get("name") == "Temperature_Celsius":
                    health_info["temperature"] = attr.get("value")
                elif attr.get("name") == "Power_On_Hours":
                    health_info["power_on_hours"] = attr.get("value")

            smart_status = data.get("smart_status")
            if smart_status and smart_status.get("passed") is False:
                health_info["health_status"] = "failing"
            elif smart_status and smart_status.get("passed") is True:
                health_info["health_status"] = "ok"
            else:
                health_info["health_status"] = "unknown"

            return health_info

        except Exception as e:
            if isinstance(e, DiskError):
                raise
            return {"temperature": None, "power_on_hours": None, "health_status": "unknown"}

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
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise DiskError(f"Disk with id {disk_id} not found")
        if disk.is_os_disk:
            raise DiskError("Cannot wipe OS disk")

        device_path = get_device_path(disk)
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

    def _parse_size(self, size_str: str) -> int:
        """Parse size string to bytes."""
        multipliers = {
            'K': 1024,
            'M': 1024**2,
            'G': 1024**3,
            'T': 1024**4,
            'P': 1024**5
        }

        size_str = size_str.strip()
        if not size_str or size_str == '0':
            return 0

        multiplier = 1
        if size_str[-1] in multipliers:
            multiplier = multipliers[size_str[-1]]
            size_str = size_str[:-1]

        try:
            return int(float(size_str) * multiplier)
        except ValueError:
            return 0

    def _determine_disk_type(self, device: Dict[str, Any]) -> str:
        """Determine disk type from device information."""
        name = (device.get("name") or "").lower()
        model = (device.get("model") or "").lower()

        if "nvme" in name or "nvme" in model:
            return "nvme"
        if device.get("rota") == 0 or "ssd" in model:
            return "ssd"
        return "hdd"


# Singleton instance
disk_manager = DiskManager()
