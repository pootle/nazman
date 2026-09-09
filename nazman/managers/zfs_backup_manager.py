from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import json
import os
import re
import time
import uuid

from sqlalchemy.orm import Session

from ..config import get_settings
from ..utils.commands import run_command, run_zfs, run_zpool, run_pipeline
from ..utils.exceptions import BackupError, ValidationError
from ..utils.validation import validate_dataset_name
from ..managers.disk_manager import (
    get_device_path, read_slot_uuids, resolve_slot_to_device, partition_by_id,
    get_os_reserved_partition_names,
)
from ..models.disk import Disk
from ..models.backup_zfs import BackupDisk, BackupSchedule, BackupRun
from ..models.scheduler import ScheduledTask, TaskType

# Marker prefix for backup anchor snapshots so they are distinct from the
# scheduler's auto-* snapshots and never touched by generic snapshot retention.
BACKUP_SNAP_PREFIX = "backup-"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _zfspath(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _is_under(child: Path, parent: Path) -> bool:
    """Check if child path is under parent (both resolved to absolute)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


class ZfsBackupManager:
    """Backup ZFS datasets (and app config) to a declared, formatted disk.

    Backups are ZFS snapshot streams written to files (gzip -6 compressed):
      full:  zfs send -R <ds>@backup-<ts> | gzip -6 > full-<ts>.zfs.gz
      incr:  zfs send -R -i <ds>@backup-<prev> <ds>@backup-<ts> | gzip -6 > incr-<ts>.zfs.gz

    Incremental backups require the previous ``backup-*`` snapshot ("anchor")
    to still exist on the source; the engine keeps the most recent anchor and
    prunes it only after the next incremental is successfully written.
    """

    def __init__(self):
        self.settings = get_settings()

    # ── Backup disk declaration / formatting ─────────────────────────────
    async def get_mount_base(self) -> Path:
        base = Path(self.settings.backup_mount_base)
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _dev_path(self, rec: BackupDisk) -> Optional[str]:
        """Derive the partition's by-id path from the physical disk identity.

        The path is never stored; it follows from ``disks.by_id`` (the OS
        identity) plus the recorded partition number.  Falls back to None when
        the disk has no by-id, which makes a probe report the disk ``offline``.
        """
        if not rec.disk:
            return None
        return partition_by_id(rec.disk.by_id, rec.partition_number or 1)

    async def _probe_state(self, rec: BackupDisk) -> Dict[str, Any]:
        """Compute availability + capacity live (nothing is persisted).

        Availability: ``mounted`` / ``full`` / ``unmounted`` / ``mismatch`` /
        ``offline``.  Capacity is taken from the mounted filesystem when
        mounted, otherwise the physical disk size with free=0.
        """
        state = await self._probe_device(rec)
        total = free = 0
        if state == "mounted":
            try:
                st = os.statvfs(rec.mount_point)
                total = st.f_frsize * st.f_blocks
                free = st.f_frsize * st.f_bavail
                if free < (1 << 20):
                    state = "full"
            except OSError:
                state = "mounted"
        elif rec.disk and rec.disk.size_bytes:
            total = rec.disk.size_bytes
        return {"status": state, "total_bytes": total, "free_bytes": free}

    async def _serialize_now(self, rec: BackupDisk) -> Dict[str, Any]:
        """Full response view of a backup disk with freshly computed state."""
        return {**self._to_dict(rec), **await self._probe_state(rec)}

    def _to_dict(self, rec: BackupDisk) -> Dict[str, Any]:
        return {
            "id": rec.id,
            "disk_id": rec.disk_id,
            "slot_uuid": rec.slot_uuid,
            "partition_number": rec.partition_number,
            "device_path": self._dev_path(rec),
            "label": rec.label,
            "fs_type": rec.fs_type,
            "mount_point": rec.mount_point,
            "fs_uuid": rec.fs_uuid,
            "unmount_after_backup": rec.unmount_after_backup,
        }

    async def list_backup_disks(self, db: Session) -> List[Dict[str, Any]]:
        disks = db.query(BackupDisk).order_by(BackupDisk.id).all()
        return [await self._serialize_now(d) for d in disks]

    async def _probe_device(self, rec: BackupDisk) -> str:
        """Determine the disk's current state without mounting it.

        Returns one of: ``mounted``, ``unmounted``, ``offline``, ``mismatch``.
        ``offline`` means the by-id device path is gone (unplugged); ``mismatch``
        means a different device is present at that path than the declared
        filesystem UUID.
        """
        if Path(rec.mount_point).is_mount():
            return "mounted"
        dev = self._dev_path(rec)
        if not dev or not os.path.exists(dev):
            return "offline"
        if rec.fs_uuid:
            current = await self._fs_uuid(dev)
            if not current or current != rec.fs_uuid:
                return "mismatch"
        return "unmounted"

    async def _unmount_rec(self, rec: BackupDisk) -> bool:
        """Unmount the disk if currently mounted; True if it ends unmounted."""
        if not Path(rec.mount_point).is_mount():
            return True
        await run_command(["umount", rec.mount_point], timeout=60, check=False, op="write", category="disk")
        return not Path(rec.mount_point).is_mount()

    async def _restore_idle_state(self, rec: BackupDisk) -> None:
        """Unmount the disk after a backup/restore/list cycle when configured."""
        if not rec or not rec.unmount_after_backup:
            return
        await self._unmount_rec(rec)

    async def declare_backup_disk(
        self, db: Session, disk_id: int, confirm: bool = False,
        slot_uuid: Optional[str] = None, label: Optional[str] = None,
        wipe_raid: bool = False,
    ) -> Dict[str, Any]:
        """Declare a disk or partition as a backup target: validate, format, mount.

        ``confirm`` mirrors the destructive-action guard used elsewhere in the
        UI (the caller must send confirm=True to allow the wipe+format).
        With ``slot_uuid`` the named ZFS-style partition is formatted in place
        (its GPT PARTLABEL survives); without it the whole disk is wiped to a
        single ext4 partition.  ``wipe_raid`` authorises stopping software RAID
        arrays and zeroing their superblocks on the target device(s).
        """
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise ValidationError("Disk not found")
        if disk.is_os_disk:
            raise ValidationError("Cannot use the OS disk as a backup disk")
        if not confirm:
            raise ValidationError("Destructive action requires confirmation")

        from .zfs_manager import zfs_manager
        pool_members = await zfs_manager.get_pool_members()
        member_pool = self._pool_member_for_disk(pool_members, disk)
        if member_pool:
            raise ValidationError(f"Disk is a member of pool '{member_pool}'; remove it from the pool first")

        if db.query(BackupDisk).filter(BackupDisk.disk_id == disk_id).first():
            raise ValidationError("Disk is already declared as a backup disk")

        if slot_uuid:
            part_dev = await self._resolve_partition(disk, slot_uuid)
            member_pool = self._pool_member_for_device(pool_members, part_dev)
            if member_pool:
                raise ValidationError(f"Partition is a member of pool '{member_pool}'; remove it from the pool first")
            await self._handle_raid([part_dev], wipe_raid)
            await self._ensure_unused(part_dev)
            # Format only the partition; keep the GPT so the slot UUID survives.
            await self._run_destructive(["wipefs", "-a", part_dev], 120, "wipe the partition")
            await self._run_destructive(["mkfs.ext4", "-F", part_dev], 600, "format the partition")
            device_path = part_dev
            partition_number = self._partition_number(part_dev)
        else:
            dev = get_device_path(disk)
            if not dev:
                raise ValidationError("Disk is not currently present")

            devices = [dev] + await self._disk_partition_paths(dev)
            await self._handle_raid(devices, wipe_raid)
            await self._ensure_unused(dev)
            # Wipe and create a single GPT partition covering the whole disk.
            await self._run_destructive(["wipefs", "-a", dev], 120, "wipe existing signatures")
            await self._run_destructive(["parted", "-s", dev, "mklabel", "gpt"], 120, "create the GPT partition table")
            await self._run_destructive(["parted", "-s", dev, "mkpart", "primary", "0%", "100%"], 120, "create the partition")
            # Let the kernel see the new partition.
            await self._run_destructive(["partprobe", dev], 120, "rescan the partition table")

            device_path = self._whole_partition_device(disk, dev)
            await self._run_destructive(["mkfs.ext4", "-F", device_path], 600, "format the partition")
            partition_number = 1

        # Read back the filesystem UUID for deterministic remounting.
        fs_uuid = await self._fs_uuid(device_path)
        if not fs_uuid:
            raise BackupError("Could not read filesystem UUID after formatting")

        mount_base = await self.get_mount_base()
        mount_point = str(mount_base / fs_uuid)
        Path(mount_point).mkdir(parents=True, exist_ok=True)
        await run_command(["mount", device_path, mount_point], timeout=60, check=False, op="write", category="disk")

        rec = BackupDisk(
            disk_id=disk_id,
            slot_uuid=slot_uuid,
            partition_number=partition_number,
            label=label,
            fs_type="ext4",
            mount_point=mount_point,
            fs_uuid=fs_uuid,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        if rec.unmount_after_backup:
            await self._unmount_rec(rec)
        return await self._serialize_now(rec)

    def _partition_number(self, dev: str) -> int:
        """Parse the partition number from a by-id (-partN) or kernel path."""
        m = re.search(r"-part(\d+)$", dev)
        if m:
            return int(m.group(1))
        name = Path(dev).name
        m = re.search(r"(?:p?)(\d+)$", name)
        if m:
            return int(m.group(1))
        return 1

    async def _resolve_partition(self, disk: Disk, slot_uuid: str) -> str:
        """Resolve a slot UUID to the partition's by-id device path.

        Raises ValidationError if the disk is absent or the partition is gone.
        """
        dev = get_device_path(disk)
        if not dev:
            raise ValidationError("Disk is not currently present")
        info = await read_slot_uuids([dev])
        parts = info.get(dev, {}).get("partitions", [])
        part_dev = resolve_slot_to_device(disk.by_id, slot_uuid, parts)
        if not part_dev:
            raise ValidationError(f"Partition with slot UUID {slot_uuid} not found on disk")
        return part_dev

    def _whole_partition_device(self, disk: Disk, dev: str) -> str:
        """Return the by-id path of partition 1 of ``dev`` if resolvable."""
        name = Path(dev).name
        if re.search(r"p\d+$", name):
            part_name = f"{name}p1"
        elif name.startswith("nvme"):
            part_name = f"{name}p1"
        else:
            part_name = f"{name}1"
        by_id = disk.by_id or ""
        if by_id:
            return f"{by_id}-part1"
        return f"/dev/{part_name}"

    @staticmethod
    def _pool_member_for_disk(pool_members: Dict[str, str], disk: Disk) -> Optional[str]:
        """Return the pool that owns ``disk`` (whole-disk or any partition)."""
        if not disk.by_id:
            return None
        by_id = disk.by_id
        basename = by_id.rsplit("/", 1)[-1]
        for key in (by_id, basename):
            if key in pool_members:
                return pool_members[key]
        for dev, pool in pool_members.items():
            if dev.startswith((f"{by_id}-part", f"{basename}-part")):
                return pool
        return None

    @staticmethod
    def _pool_member_for_device(pool_members: Dict[str, str], dev: str) -> Optional[str]:
        """Return the pool that owns the exact device ``dev`` (path or basename)."""
        for key in (dev, dev.rsplit("/", 1)[-1]):
            if key in pool_members:
                return pool_members[key]
        return None

    async def _ensure_unused(self, dev: str) -> None:
        """Refuse to destroy a device whose partitions are live (md/mount/swap).

        ``dev`` is the whole-disk or partition device about to be wiped.  A
        device can only be formatted once nothing (software RAID, a mount, or
        swap) is holding it; stopping the holder is the caller's job.
        """
        stdout, _, rc = await run_command(
            ["lsblk", "-J", "-o", "NAME,TYPE,PKNAME,MOUNTPOINT", dev], timeout=10,
            check=False, op="read", category="disk",
        )
        if rc != 0 or not stdout.strip():
            # Cannot inspect the device; let the destructive command fail loudly.
            return
        try:
            data = json.loads(stdout)
        except ValueError:
            return

        holders: List[str] = []
        seen: set = set()

        def walk(devices: List[Dict[str, Any]]) -> None:
            for node in devices:
                name = node.get("name") or ""
                if name in seen:
                    continue
                seen.add(name)
                if node.get("type") == "md":
                    holders.append(f"software RAID {name}")
                mp = node.get("mountpoint")
                if mp:
                    holders.append(f"mounted at {mp}")
                walk(node.get("children", []))

        walk(data.get("blockdevices", []))
        if holders:
            raise ValidationError(
                f"{dev} is in use ({', '.join(sorted(set(holders)))}); "
                "stop the holder and retry"
            )

    async def _disk_partition_paths(self, dev: str) -> List[str]:
        """Return the kernel device paths of all partitions on ``dev``."""
        info = await read_slot_uuids([dev])
        return [f"/dev/{p['name']}" for p in info.get(dev, {}).get("partitions", [])]

    async def _handle_raid(self, devices: List[str], wipe_raid: bool) -> None:
        """Deal with software RAID metadata on the devices about to be wiped.

        Refuses (with an explicit pointer to the ``wipe_raid`` option) when a
        superblock is present but the caller has not authorised wiping it.
        Otherwise stops the owning arrays and zeroes every superblock so the
        device is genuinely free (including stale superblocks of another
        version, which would otherwise reassemble after a reboot).
        """
        superblocks = await self._md_superblocks(devices)
        if not superblocks:
            return
        detail = "; ".join(
            f"{s['device']} (array {s['name'] or 'unknown'}, version {s['version'] or '?'})"
            for s in superblocks
        )
        if not wipe_raid:
            raise ValidationError(
                f"{devices[0]} carries software RAID metadata ({detail}); "
                "confirm wiping the RAID info to proceed"
            )
        await self._wipe_raid_metadata(devices, superblocks)

    async def _md_superblocks(self, devices: List[str]) -> List[Dict[str, Any]]:
        """Probe ``mdadm --examine`` for every device; return matching entries."""
        os_names = await get_os_reserved_partition_names()
        found = []
        for dev in devices:
            stdout, _, rc = await run_command(
                ["mdadm", "--examine", dev], timeout=15, check=False,
                op="read", category="disk",
            )
            if rc != 0 or not stdout.strip():
                continue
            info = self._parse_mdadm_examine(stdout)
            if not info:
                continue
            info["device"] = dev
            info["os_backing"] = Path(dev).name in os_names
            found.append(info)
        return found

    @staticmethod
    def _parse_mdadm_examine(stdout: str) -> Optional[Dict[str, str]]:
        """Extract key/value lines from ``mdadm --examine`` output."""
        fields: Dict[str, str] = {}
        for line in stdout.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            val = val.strip()
            if val:
                fields[key.strip()] = val
        if not fields:
            return None
        return {
            "name": fields.get("Name") or fields.get("Raid Device"),
            "version": fields.get("Version"),
        }

    async def _wipe_raid_metadata(
        self, devices: List[str], superblocks: List[Dict[str, Any]]
    ) -> None:
        """Stop arrays on ``devices`` and zero their superblocks.

        Arrays that back the OS are never touched; stopping them would take
        the system down.
        """
        for sb in superblocks:
            if sb.get("os_backing"):
                raise ValidationError(
                    f"Cannot wipe RAID metadata on {sb['device']} (array "
                    f"{sb.get('name') or 'unknown'}): it is part of the OS and "
                    f"must be handled manually"
                )

        md_names = await self._md_children(devices)
        for md_name in md_names:
            # The array may already be stopped (stale superblock); ignore rc.
            await run_command(
                ["mdadm", "--stop", f"/dev/{md_name}"], timeout=30, check=False,
                op="write", category="disk",
            )
        for sb in superblocks:
            await self._run_destructive(
                ["mdadm", "--zero-superblock", sb["device"]], 30, "wipe RAID metadata"
            )

    async def _md_children(self, devices: List[str]) -> List[str]:
        """Return the names of any md arrays built on ``devices`` (lsblk)."""
        md_names = set()
        for dev in devices:
            stdout, _, rc = await run_command(
                ["lsblk", "-J", "-o", "NAME,TYPE", dev], timeout=10, check=False,
                op="read", category="disk",
            )
            if rc != 0 or not stdout.strip():
                continue
            try:
                data = json.loads(stdout)
            except ValueError:
                continue

            def walk(nodes: List[Dict[str, Any]]) -> None:
                for node in nodes:
                    if node.get("type") == "md":
                        md_names.add(node.get("name") or "")
                    walk(node.get("children", []))

            walk(data.get("blockdevices", []))
        return sorted(m for m in md_names if m)

    async def get_raid_info(
        self, db: Session, disk_id: int, slot_uuid: Optional[str] = None
    ) -> Dict[str, Any]:
        """Software RAID metadata found on a disk/partition (for the UI probe)."""
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise ValidationError("Disk not found")
        if slot_uuid:
            dev = await self._resolve_partition(disk, slot_uuid)
            devices = [dev]
        else:
            dev = get_device_path(disk)
            if not dev:
                raise ValidationError("Disk is not currently present")
            devices = [dev] + await self._disk_partition_paths(dev)
        return {"device": dev, "md": await self._md_superblocks(devices)}

    async def _run_destructive(self, cmd: List[str], timeout: int, action: str) -> None:
        """Run a destructive/changing command, failing loudly with stderr."""
        try:
            await run_command(cmd, timeout=timeout, op="write", category="disk")
        except Exception as e:
            raise BackupError(f"Could not {action}: {e}")

    async def _fs_uuid(self, dev: str) -> Optional[str]:
        stdout, _, rc = await run_command(
            ["blkid", "-s", "UUID", "-o", "value", dev], timeout=30, check=False, op="read", category="disk"
        )
        return stdout.strip() or None

    # -- device wake -------------------------------------------------------

    def _mounted_block_devices(self) -> set:
        """Kernel base names of every block device currently holding a mount."""
        mounted = set()
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                dev = line.split(" ")[0] or ""
                if dev.startswith("/dev/"):
                    mounted.add(os.path.basename(dev))
        except OSError:
            pass
        return mounted

    def _usb_storage_bridges(self) -> List[Path]:
        """USB device dirs that expose a mass-storage bridge and host no mount."""
        usb = Path("/sys/bus/usb/devices")
        if not usb.is_dir():
            return []
        mounted = self._mounted_block_devices()
        bridges = []
        for path in usb.iterdir():
            if not path.is_dir() or not (path / "authorized").exists():
                continue
            try:
                vendor = (path / "idVendor").read_text().strip().lower()
            except OSError:
                continue
            if vendor != "152d" and vendor != "0bda":
                continue
            # Skip bridges currently steering a mounted filesystem: re-plugging
            # one would yank it out from under a live mount.
            if self._bridge_has_mounted_device(path, mounted):
                continue
            bridges.append(path)
        return bridges

    def _bridge_has_mounted_device(self, bus_dir: Path, mounted: set) -> bool:
        for root, dirs, files in os.walk(str(bus_dir)):
            if os.path.basename(root) == "block":
                for entry in dirs:
                    if entry in mounted:
                        return True
        return False

    async def _wake_backup_disk(self, rec: BackupDisk) -> bool:
        """Try to bring a missing backup disk back without a power cycle.

        1. Drive present (device path resolves): it is only asleep at the ATA
           level, so poke it with ``hdparm -C`` and wait for spin-up.
        2. Drive missing but its USB bridge is still on the bus: toggle the
           bridge's ``authorized`` file to force a kernel-side re-enumeration
           (a software replug), then poll for the device to reappear.
        3. Bridge not on the bus at all: nothing to talk to; return False so
           the caller reports that a power cycle is required.
        """
        dev = self._dev_path(rec)
        if dev and os.path.exists(dev):
            await run_command(["hdparm", "-C", dev], timeout=30,
                              check=False, op="read", category="disk")
            await asyncio.sleep(2)
            return bool(dev and os.path.exists(dev))

        toggled = False
        for bus_dir in self._usb_storage_bridges():
            authorized = bus_dir / "authorized"
            try:
                authorized.write_text("0")
                await asyncio.sleep(1)
                authorized.write_text("1")
                toggled = True
            except OSError:
                continue
        if not toggled:
            return False

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if dev and os.path.exists(dev):
                await asyncio.sleep(2)
                return True
            await asyncio.sleep(1)
        return False

    async def wake_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        """Public wake/replug action: force the disk present again and re-probe."""
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        state = await self._probe_device(rec)
        if state == "offline":
            if not await self._wake_backup_disk(rec):
                raise BackupError(
                    "Backup disk could not be woken by software; power-cycle its enclosure"
                )
        return await self._serialize_now(rec)

    async def mount_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        state = await self._probe_device(rec)
        if state == "mounted":
            return await self._serialize_now(rec)
        if state == "offline":
            # The disk may be asleep or its bridge idle-but-enumerated; try to
            # bring it back before giving up so scheduled/manual backups can
            # proceed without the user touching the enclosure.
            await self._wake_backup_disk(rec)
            state = await self._probe_device(rec)
        if state == "offline":
            raise BackupError(
                "Backup disk is not connected; a software wake was attempted. "
                "If the enclosure is fitted, power-cycle it."
            )
        if state == "mismatch":
            raise BackupError(
                "Filesystem changed: the disk present is not the declared backup "
                "filesystem (UUID differs). Re-scan or re-declare."
            )
        Path(rec.mount_point).mkdir(parents=True, exist_ok=True)
        _, _, rc = await run_command(
            ["mount", self._dev_path(rec), rec.mount_point], timeout=60, check=False, op="write", category="disk"
        )
        if rc != 0 or not Path(rec.mount_point).is_mount():
            raise BackupError("Failed to mount backup disk")
        return await self._serialize_now(rec)

    async def unmount_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if not await self._unmount_rec(rec):
            raise BackupError("Failed to unmount backup disk")
        return await self._serialize_now(rec)

    async def scan_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        return await self._serialize_now(rec)

    async def deregister_backup_disk(self, db: Session, backup_disk_id: int) -> None:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if Path(rec.mount_point).is_mount():
            try:
                await run_command(["umount", rec.mount_point], timeout=60, check=False, op="write", category="disk")
            except Exception:
                pass
        # The runs/schedules for this disk reference stream files stored on it;
        # with the disk deregistered those records are meaningless, so remove
        # them (the FK cascade also covers this when foreign_keys is enabled).
        db.query(BackupRun).filter(BackupRun.backup_disk_id == backup_disk_id).delete()
        db.query(BackupSchedule).filter(BackupSchedule.backup_disk_id == backup_disk_id).delete()
        db.delete(rec)
        db.commit()

    # ── Capacity estimation -------------------------------------------------
    async def estimate_full_size(self, dataset_name: str) -> int:
        """Estimated raw (uncompressed stream) size of a full backup = used bytes."""
        stdout, _, rc = await run_zfs(
            "get", "-Hp", "-o", "value", "used", dataset_name, check=False, op="read",
        )
        if rc != 0:
            return 0
        try:
            return int(stdout.strip())
        except ValueError:
            return 0

    async def _dataset_exists(self, dataset_name: str) -> bool:
        """Confirm a dataset currently exists in ZFS by its full name."""
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", dataset_name, check=False, op="read",
        )
        return rc == 0 and dataset_name in stdout.split()

    async def estimate_incremental_size(self, db: Session, dataset_name: str) -> int:
        """Estimate incr size: last incremental's changed_bytes, else 10% of used."""
        last = (
            db.query(BackupRun)
            .filter(
                BackupRun.dataset_name == dataset_name,
                BackupRun.backup_type == "incremental",
                BackupRun.status == "success",
            )
            .order_by(BackupRun.id.desc())
            .first()
        )
        if last and last.changed_bytes:
            return last.changed_bytes
        used = await self.estimate_full_size(dataset_name)
        return int(used * 0.1) if used else 0

    async def check_capacity(self, db: Session, backup_disk_id: int, needed_bytes: int) -> bool:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if not Path(rec.mount_point).is_mount():
            raise BackupError("Backup disk is not mounted; cannot check capacity")
        st = os.statvfs(rec.mount_point)
        free = st.f_frsize * st.f_bavail
        return free >= needed_bytes

    # ── Backup engine -------------------------------------------------------
    async def run_backup(
        self,
        db: Session,
        dataset_name: str,
        backup_disk_id: int,
        backup_type: str = "full",
    ) -> BackupRun:
        """Run a full or incremental backup of a dataset to a backup disk."""
        if backup_type not in ("full", "incremental"):
            raise ValidationError("backup_type must be 'full' or 'incremental'")

        # The dataset is identified by its ZFS name; confirm it exists in ZFS.
        ok = await self._dataset_exists(dataset_name)
        if not ok:
            raise ValidationError(f"Dataset '{dataset_name}' not found")
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")

        run = BackupRun(
            dataset_name=dataset_name,
            backup_disk_id=backup_disk_id,
            backup_type=backup_type,
            snapshot="",
            stream_file="",
            status="running",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        snap = None
        try:
            await self.mount_backup_disk(db, backup_disk_id)

            snap = f"{dataset_name}@{BACKUP_SNAP_PREFIX}{_ts()}"
            await run_zfs("snapshot", "-r", snap, timeout=120, check=True)

            base_snapshot = None
            full_anchor = None
            if backup_type == "incremental":
                base_snapshot = await self._find_anchor(dataset_name)
                if base_snapshot is None:
                    # No anchor -> promote to a full backup automatically.
                    backup_type = "full"
                else:
                    full_anchor = await self._find_full_anchor(dataset_name, base_snapshot)

            dest_dir = self._dataset_dir(rec.mount_point, dataset_name)
            dest_dir.mkdir(parents=True, exist_ok=True)
            suffix = "zfs.gz"
            if backup_type == "full":
                file_name = f"full-{self._snap_ts(snap)}.{suffix}"
                send_cmd = ["zfs", "send", "-R", snap]
            else:
                file_name = f"incr-{self._snap_ts(snap)}.{suffix}"
                send_cmd = ["zfs", "send", "-R", "-i", base_snapshot, snap]
            stream_file = str(dest_dir / file_name)

            # Capacity guard: estimate needed space vs free space.
            if backup_type == "full":
                needed = await self.estimate_needed(dataset_name)
                run.changed_bytes = 0  # full backups report changed=0; UI uses entire stream
            else:
                needed = await self.estimate_incremental_size(db, dataset_name)
            if not await self.check_capacity(db, backup_disk_id, needed):
                await run_zfs("destroy", "-r", snap, timeout=60, check=False)
                run.status = "failed"
                run.error = "Insufficient free space on backup disk"
                db.commit()
                return run

            gzip_level = int(self.settings.backup_gzip_level)
            _, stderr, rc = await run_pipeline(
                [send_cmd, ["gzip", f"-{gzip_level}"]],
                stdout_path=stream_file,
                timeout=86400, check=False, op="write", category="zfs",
            )
            if rc != 0:
                Path(stream_file).unlink(missing_ok=True)
                await run_zfs("destroy", "-r", snap, timeout=60, check=False)
                run.status = "failed"
                run.error = stderr or "zfs send failed"
                db.commit()
                return run

            size_bytes = Path(stream_file).stat().st_size if Path(stream_file).exists() else 0

            # Any remaining bytes are "changed data"; for a full it's the whole stream.
            run.backup_type = backup_type
            run.snapshot = snap
            run.base_snapshot = base_snapshot
            run.full_anchor = full_anchor
            run.stream_file = stream_file
            run.size_bytes = size_bytes
            if backup_type == "incremental":
                run.changed_bytes = size_bytes
            run.status = "success"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()

            # Prune old source anchors now that this backup is safely written.
            if backup_type == "incremental" and base_snapshot:
                await self._prune_old_anchors(dataset_name, snap)
            else:
                await self._prune_old_anchors(dataset_name, snap, keep_full=snap)
            return run

        except Exception as e:
            if snap and not run.snapshot:
                await run_zfs("destroy", "-r", snap, timeout=60, check=False)
            run.status = "failed"
            run.error = str(e)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return run
        finally:
            await self._restore_idle_state(rec)

    async def estimate_needed(self, dataset_name: str) -> int:
        """Needed bytes for a full backup of a dataset (with safety margin)."""
        used = await self.estimate_full_size(dataset_name)
        return int(used * self.settings.backup_full_margin) if used else 0

    async def _find_anchor(self, dataset_name: str) -> Optional[str]:
        """Return the most recent backup-* snapshot of dataset to use as incr base."""
        snaps = await self._list_backup_snapshots(dataset_name)
        return snaps[-1] if snaps else None  # name sort ~ creation order for fixed-width ts

    async def _find_full_anchor(self, dataset_name: str, base_snapshot: str) -> Optional[str]:
        """Return the full snapshot this incremental chain derives from (the earliest backup-*)."""
        snaps = await self._list_backup_snapshots(dataset_name)
        return snaps[0] if snaps else None

    async def _list_backup_snapshots(self, dataset_name: str) -> List[str]:
        """List backup-* snapshots of the dataset itself (not children)."""
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", "-t", "snapshot", dataset_name, check=False, op="read",
        )
        if rc != 0:
            return []
        names = []
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "@" not in line:
                continue
            ds, snap = line.split("@", 1)
            if ds != dataset_name:
                continue
            if snap.startswith(BACKUP_SNAP_PREFIX):
                names.append(line)
        names.sort()
        return names

    async def _prune_old_anchors(self, dataset_name: str, keep: str, keep_full: Optional[str] = None) -> None:
        """Destroy backup-* snapshots older than the one just created, keeping
        the newest (and optionally the full-chain start) as anchors."""
        snaps = await self._list_backup_snapshots(dataset_name)
        exempt = {keep}
        if keep_full:
            exempt.add(keep_full)
        # Keep newest N (small safety buffer) plus the exempt full anchor.
        newest = set(snaps[-2:])
        for s in snaps:
            if s in exempt or s in newest:
                continue
            await run_zfs("destroy", "-r", s, timeout=60, check=False)

    def _dataset_dir(self, mount_point: str, dataset_name: str) -> Path:
        return Path(mount_point) / "data" / dataset_name

    def _snap_ts(self, snapshot: str) -> str:
        return snapshot.rsplit("@", 1)[-1].replace(BACKUP_SNAP_PREFIX, "")
    # -- schedule synchronization ---------------------------------------------
    async def sync_scheduled_tasks(self, db: Session) -> None:
        """Reconcile backup_schedules rows into ScheduledTask (ZFS_BACKUP) jobs.

        Called on scheduler startup so scheduled full/incremental backups survive
        restarts, and used by the API when a schedule is saved or removed.
        """
        from .scheduler import scheduler_manager

        # Names map uniquely back to their schedule row (dataset + disk + type).
        schedules = db.query(BackupSchedule).all()
        desired: Dict[str, Dict] = {}
        for s in schedules:
            if not s.enabled:
                continue
            base_cfg = {
                "dataset_name": s.dataset_name,
                "backup_disk_id": s.backup_disk_id,
                "type": "full",
            }
            if s.full_cron:
                desired[f"zfs-full-{s.dataset_name}-{s.backup_disk_id}"] = {
                    **base_cfg, "cron": s.full_cron, "type": "full", "retention": s.full_retention}
            if s.incremental_cron:
                desired[f"zfs-incr-{s.dataset_name}-{s.backup_disk_id}"] = {
                    **base_cfg, "cron": s.incremental_cron, "type": "incremental", "retention": s.incremental_retention}

        existing = {t.name: t for t in db.query(ScheduledTask).filter(
            ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()}

        for name, cfg in desired.items():
            sched_cron = cfg["cron"]
            config = {k: cfg[k] for k in ("dataset_name", "backup_disk_id", "type", "retention")}
            task = existing.get(name)
            if task is None:
                await scheduler_manager.create_task(
                    db, name=name, task_type=TaskType.ZFS_BACKUP,
                    target=str(cfg["dataset_name"]), schedule=sched_cron, config=config,
                )
            else:
                if task.schedule != sched_cron or task.config != config:
                    await scheduler_manager.update_task(
                        db, task.id, schedule=sched_cron, config=config,
                    )

        # Remove tasks whose schedule row is gone or disabled.
        for name, task in existing.items():
            if name not in desired:
                await scheduler_manager.delete_task(db, task.id)

    # -- restore -------------------------------------------------------------

    async def list_stream_files(self, db: Session, backup_disk_id: int) -> List[Dict[str, Any]]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        try:
            if not Path(rec.mount_point).is_mount():
                await self.mount_backup_disk(db, backup_disk_id)
            base = Path(rec.mount_point) / "data"
            files = []
            if base.exists():
                for p in sorted(base.rglob("*.zfs.gz")):
                    files.append({
                        "path": str(p),
                        "dataset": str(p.relative_to(base)).split("/")[0],
                        "size_bytes": p.stat().st_size,
                    })
            return files
        finally:
            await self._restore_idle_state(rec)

    async def restore_dataset(
        self, db: Session, stream_file: str, target_dataset: str, force: bool = False
    ) -> Dict[str, Any]:
        """Restore a dataset from a stream file.

        The stream file must reside under a registered backup disk's data directory.
        By default the target dataset must not exist; pass ``force=True`` to allow
        overwriting an existing dataset (equivalent to ``zfs receive -F``).
        """
        validate_dataset_name(target_dataset)

        fp = Path(stream_file).resolve()

        # Locate the registered backup disk whose data directory owns this path.
        owner = None
        for rec in db.query(BackupDisk).all():
            data_dir = (Path(rec.mount_point) / "data").resolve()
            if _is_under(fp, data_dir):
                owner = rec
                break
        if owner is None:
            raise BackupError(
                "Stream file must reside under a registered backup disk's data directory"
            )

        if not Path(owner.mount_point).is_mount():
            await self.mount_backup_disk(db, owner.id)
        if not fp.exists():
            raise BackupError(f"Stream file not found: {stream_file}")

        allowed_bases = []
        for rec in db.query(BackupDisk).all():
            data_dir = Path(rec.mount_point) / "data"
            if data_dir.exists():
                allowed_bases.append(data_dir.resolve())

        if not any(_is_under(fp, base) for base in allowed_bases):
            raise BackupError(
                f"Stream file must reside under a registered backup disk's data directory"
            )

        receive_cmd = ["zfs", "receive"]
        if force:
            receive_cmd.append("-F")
        receive_cmd.append(target_dataset)

        try:
            _, stderr, rc = await run_pipeline(
                [["gunzip", "-c", str(fp)], receive_cmd],
                timeout=86400, check=False, op="write", category="zfs",
            )
        finally:
            await self._restore_idle_state(owner)
        if rc != 0:
            raise BackupError(f"Restore failed: {stderr}")
        return {"dataset": target_dataset, "source": str(fp), "force": force}


def shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)


zfs_backup_manager = ZfsBackupManager()
