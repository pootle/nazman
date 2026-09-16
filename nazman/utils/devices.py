"""Stable device identity helpers (shared by disk, zfs and backup subsystems).

Disk identity is anchored on ``/dev/disk/by-id/`` paths; ephemeral kernel
names (``sda``, ``nvme0n1``) are tracked in an in-memory map that is rebuilt on
every scan.  Partition slots are marked with a ``nazman:<uuid>`` GPT PARTLABEL.
This module lives in utils so managers never need to import each other for
device plumbing.
"""

from typing import List, Optional, Dict, Any
import json
import os
import re

from .commands import run_command
from .sizes import parse_size_to_bytes

# In-memory registry of ephemeral kernel names, keyed by stable identity.
# device_name/device_path are NOT persisted: /dev/sdX changes on boot and
# hot-plug, so we rebuild this map (from lsblk + by-id resolution) whenever the
# service starts or a scan runs.  Commands that need a live device path look it
# up here rather than in the database.
_device_map: Dict[str, Dict[str, Any]] = {}  # by_id -> {kernel_name, device_path}
_serial_device_map: Dict[str, Dict[str, Any]] = {}  # serial -> {kernel_name, device_path}

BY_ID_DIR = "/dev/disk/by-id"

# Preferred by-id prefixes, in order. Model+serial based IDs (ata/nvme/mmc) are
# the most stable and human-readable; wwn/scsi are fallbacks.
_SAFE_PREFIXES = ["ata-", "nvme-", "mmc-", "scsi-", "wwn-", "dm-"]

# eMMC boot/RPMB hardware sub-devices (mmcblk0boot0, mmcblk0boot1, mmcblk0rpmb).
# They inherit the parent eMMC's serial/by-id and cannot be uniquely identified,
# so they are excluded from disk discovery.
MMC_SUBDEVICE_RE = re.compile(r"^mmcblk[0-9]+(?:boot[0-9]+|rpmb)$")


def refresh_device_map(discovered: List[Dict[str, Any]]) -> None:
    """Rebuild the in-memory by_id/serial -> kernel path map from discovery.

    ``discovered`` entries are the transient dicts produced by disk discovery
    (which include ``device_name`` and ``device_path``).  Only entries with a
    stable identity are retained.
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


def get_device_entry(disk: Any) -> Optional[Dict[str, Optional[str]]]:
    """Return the current transient kernel name/path for a disk, or None if the
    disk is not currently present in the system."""
    if disk.by_id and disk.by_id in _device_map:
        return _device_map[disk.by_id]
    if disk.serial and disk.serial in _serial_device_map:
        return _serial_device_map[disk.serial]
    return None


def get_device_path(disk: Any) -> Optional[str]:
    entry = get_device_entry(disk)
    return entry["device_path"] if entry else None


def get_device_name(disk: Any) -> Optional[str]:
    entry = get_device_entry(disk)
    return entry["device_name"] if entry else None


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


def strip_partition_suffix(name: str) -> str:
    """Strip a partition suffix from a kernel device name.

    ``nvme0n1p2`` -> ``nvme0n1``, ``mmcblk0p1`` -> ``mmcblk0``,
    ``sda1`` -> ``sda``, ``vda2``/``hdb3`` likewise. Unknown formats are
    returned unchanged.
    """
    m = re.match(r"^(nvme\d+n\d+)p\d+$", name)
    if m:
        return m.group(1)
    m = re.match(r"^(mmcblk\d+)p\d+$", name)
    if m:
        return m.group(1)
    m = re.match(r"^([shv]d[a-z]+)\d+$", name)
    if m:
        return m.group(1)
    return name


def kernel_base_name(name: str) -> str:
    """Strip trailing partition digits (and optional 'p' prefix) generically.

    Unlike :func:`strip_partition_suffix` (which only recognises known kernel
    naming schemes), this removes any ``pN``/``N`` suffix, e.g. ``md0p1`` ->
    ``md0``.  Used when matching ZFS-reported device names to live disks.
    """
    m = re.search(r"(?:p\d+|\d+)$", name)
    return name[:m.start()] if m else name


def normalize_base_name(leaf: str) -> str:
    """Map a ZFS leaf device name to a base block device name.

    Handles kernel names (``sda1`` -> ``sda``, ``nvme0n1p1`` -> ``nvme0n1``),
    ``/dev/...`` prefixes and ``/dev/disk/by-id/...`` symlinks, and bare by-id
    alias strings (e.g. ``ata-WDC_...-part1``).
    """
    name = leaf.strip()
    if not name:
        return leaf
    if name.startswith("."):
        name = name[1:]
    leaf_bare = name.rsplit("/", 1)[-1]
    try:
        if os.path.isabs(name) and os.path.exists(name):
            return strip_partition_suffix(os.path.realpath(name).rsplit("/", 1)[-1])
    except Exception:
        pass
    for dev_dir in ("/dev", "/dev/disk/by-id"):
        try:
            p = f"{dev_dir}/{leaf_bare}"
            if os.path.exists(p):
                return strip_partition_suffix(os.path.realpath(p).rsplit("/", 1)[-1])
        except Exception:
            pass
    return strip_partition_suffix(leaf_bare)


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
                if isinstance(size_str, (int, float)):
                    size_bytes = int(size_str)
                else:
                    size_bytes = parse_size_to_bytes(str(size_str))

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


# ── OS-reserved devices ──────────────────────────────────────────────────

def _physical_disk_for_partition(part_name: str) -> Optional[str]:
    """Return the physical disk (e.g. ``nvme0n1``) that owns ``part_name``."""
    if not part_name or part_name.startswith(("md", "loop")):
        return None
    if part_name.startswith(("nvme", "mmcblk")):
        idx = part_name.rfind("p")
        return part_name[:idx] if idx != -1 and part_name[idx + 1:].isdigit() else None
    # sda1 -> sda, vda1 -> vda, xvda1 -> xvda
    base = part_name.rstrip("0123456789")
    return base or None


async def _resolve_backing_partitions(source: str) -> set:
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

    member_map = await _get_lsblk_member_map()
    members = {
        parent for parent, children in member_map.items()
        if backing in children
    }
    if members:
        names.update(members)
    else:
        names.add(src_name)
    return names


async def _get_lsblk_member_map() -> Dict[str, set]:
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


async def os_reserved_partition_names() -> set:
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
            names.update(await _resolve_backing_partitions(stdout.strip()))
    return names


async def os_disk_names() -> set:
    """Detect the physical disks the OS is running on.

    Resolves every physical disk underneath the root filesystem, correctly
    handling software RAID where an md array spans multiple physical disks
    (each member disk is identified, not just one).
    """
    os_disks = set()
    try:
        reserved = await os_reserved_partition_names()
    except Exception:
        return os_disks
    for part_name in reserved:
        disk = _physical_disk_for_partition(part_name)
        if disk:
            os_disks.add(disk)
    return os_disks
