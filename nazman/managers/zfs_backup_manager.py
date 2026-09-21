from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import json
import logging
import os
import re
import time

from sqlalchemy.orm import Session

from ..config import get_settings
from ..utils.commands import run_command, run_zfs, run_pipeline
from ..utils.exceptions import BackupError, BackupDiskNotFoundError, BackupRunNotFoundError, ValidationError
from ..utils.validation import validate_dataset_name
from ..utils.devices import (
    get_device_path, read_slot_uuids, resolve_slot_to_device, partition_by_id,
    os_reserved_partition_names, kernel_base_name,
)
from ..utils import zfs_query
from ..utils import backup_manifest as bm
from ..models.disk import Disk
from ..models.backup_zfs import BackupDisk, BackupSchedule, BackupRun
from ..models.scheduler import ScheduledTask, TaskType

logger = logging.getLogger(__name__)

# Marker prefix for backup anchor snapshots so they are distinct from the
# scheduler's auto-* snapshots and never touched by generic snapshot retention.
BACKUP_SNAP_PREFIX = "backup-"

# How long a failed in-memory declaration stays visible in the disk list.
PENDING_FAILED_TTL = 600


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

    def __init__(self, zfs=None, scheduler=None, backup=None):
        self.settings = get_settings()
        # Collaborators injected at wiring time (constructor injection keeps
        # the manager independently testable and free of import cycles).
        self.zfs = zfs
        self.scheduler = scheduler
        self.backup = backup
        self._pending_declares: Dict[int, Dict[str, Any]] = {}
        self._declare_tasks: set = set()
        self._backup_tasks: set = set()

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
        mounted, otherwise probed from the ext-family superblock (dumpe2fs) so
        the backup partition's free space is still reported while unmounted.
        When neither is possible the physical disk size is shown with free=0.
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
        elif state == "unmounted":
            usage = await self._probe_partition_usage(rec)
            if usage:
                total, free = usage
                if free < (1 << 20):
                    state = "full"
        if not total and rec.disk and rec.disk.size_bytes:
            total = rec.disk.size_bytes
        return {"status": state, "total_bytes": total, "free_bytes": free}

    async def _probe_partition_usage(self, rec: BackupDisk) -> Optional[Tuple[int, int]]:
        """Read an unmounted ext-family partition's total/free bytes via dumpe2fs.

        Returns ``(total, free)`` bytes or None when the filesystem type is not
        ext-family, the device is missing, or the probe fails.
        """
        dev = self._dev_path(rec)
        if not dev or not os.path.exists(dev):
            return None
        if (rec.fs_type or "ext4").lower() not in ("ext2", "ext3", "ext4"):
            return None
        _, out, rc = await run_command(
            ["dumpe2fs", "-h", dev], timeout=30, check=False, op="read", category="disk"
        )
        if rc != 0:
            return None
        blocks = free_blocks = block_size = None
        for line in out.splitlines():
            if "Block count:" in line:
                blocks = int(line.split(":", 1)[1].strip())
            elif "Free blocks:" in line:
                free_blocks = int(line.split(":", 1)[1].strip())
            elif "Block size:" in line:
                block_size = int(line.split(":", 1)[1].strip())
        if None in (blocks, free_blocks, block_size) or block_size <= 0:
            return None
        return blocks * block_size, free_blocks * block_size

    async def serialize_now(self, rec: BackupDisk) -> Dict[str, Any]:
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
        now = datetime.now(timezone.utc)
        pending = []
        for disk_id, entry in list(self._pending_declares.items()):
            if entry["status"] == "failed" and (now - entry["started_at"]).total_seconds() > PENDING_FAILED_TTL:
                self._pending_declares.pop(disk_id, None)
                continue
            pending.append(self._pending_to_dict(entry))
        disks = db.query(BackupDisk).order_by(BackupDisk.id).all()
        return pending + [await self.serialize_now(d) for d in disks]

    def pending_disk_ids(self) -> List[int]:
        """Disk ids with an in-flight (or recently failed) declaration."""
        return sorted(self._pending_declares.keys())

    def _pending_to_dict(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Response view of an in-memory pending/failed declaration."""
        return {k: v for k, v in entry.items() if k != "started_at"}

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

    async def start_declare_backup_disk(
        self, db: Session, disk_id: int, confirm: bool = False,
        slot_uuid: Optional[str] = None, label: Optional[str] = None,
        wipe_raid: bool = False,
    ) -> Dict[str, Any]:
        """Validate quickly, register an in-memory ``pending`` entry and launch
        the wipe/format as a background task; returns the pending view at once.

        The destructive work runs in ``_run_declare`` so the caller sees the
        new backup disk in the list immediately.  The in-memory entry is
        replaced by the real DB row on success, or flips to ``failed`` (with
        ``error``) so failures are reported without a blocking request.
        """
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise ValidationError("Disk not found")
        if disk.is_os_disk:
            raise ValidationError("Cannot use the OS disk as a backup disk")
        if not confirm:
            raise ValidationError("Destructive action requires confirmation")
        if db.query(BackupDisk).filter(BackupDisk.disk_id == disk_id).first():
            raise ValidationError("Disk is already declared as a backup disk")
        cur = self._pending_declares.get(disk_id)
        if cur and cur["status"] == "pending":
            raise ValidationError("A declaration for this disk is already in progress")

        entry = {
            "id": -disk_id,
            "disk_id": disk_id,
            "slot_uuid": slot_uuid,
            "partition_number": 1,
            "device_path": partition_by_id(disk.by_id, 1) if disk.by_id else None,
            "label": label,
            "fs_type": "ext4",
            "mount_point": "",
            "fs_uuid": "",
            "total_bytes": disk.size_bytes or 0,
            "free_bytes": 0,
            "status": "pending",
            "unmount_after_backup": True,
            "error": None,
            "started_at": datetime.now(timezone.utc),
        }
        self._pending_declares[disk_id] = entry
        task = asyncio.create_task(
            self._run_declare(disk_id, slot_uuid, label, wipe_raid)
        )
        self._declare_tasks.add(task)
        task.add_done_callback(self._declare_tasks.discard)
        return self._pending_to_dict(entry)

    async def _run_declare(
        self, disk_id: int, slot_uuid: Optional[str], label: Optional[str],
        wipe_raid: bool,
    ) -> None:
        """Background task executing the actual wipe+format declaration."""
        try:
            from ..database import get_db_context
            with get_db_context() as db:
                await self.declare_backup_disk(
                    db, disk_id, confirm=True,
                    slot_uuid=slot_uuid, label=label, wipe_raid=wipe_raid,
                )
            self._pending_declares.pop(disk_id, None)
        except Exception as e:
            entry = self._pending_declares.get(disk_id)
            if entry:
                entry["status"] = "failed"
                entry["error"] = str(e)
            logger.error("background declare failed for disk %s: %s", disk_id, e, exc_info=True)

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

        pool_members = await self._get_pool_members()
        member_pool = zfs_query.pool_member_for_disk(pool_members, disk)
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
        await self._seed_volume(db, rec)
        if rec.unmount_after_backup:
            await self._unmount_rec(rec)
        return await self.serialize_now(rec)

    async def _seed_volume(self, db: Session, rec: BackupDisk) -> None:
        """Seed a freshly declared volume with the current configuration.

        Falls back to an empty manifest when no backup manager is injected, so
        the volume is discoverable on a fresh install either way.
        """
        if self.backup is not None:
            try:
                await self.backup.capture_config_bundle(
                    db, rec.mount_point, media=self._media_identity(rec),
                )
                return
            except Exception as e:
                logger.warning("failed to seed config bundle on %s: %s", rec.mount_point, e)
        self._write_initial_manifest(rec)

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

    async def _get_pool_members(self) -> Dict[str, str]:
        """Live {device path -> pool name} map, via the injected ZfsManager."""
        if self.zfs is None:
            return {}
        return await self.zfs.get_pool_members()

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
        os_names = await os_reserved_partition_names()
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
        return await self.serialize_now(rec)

    async def mount_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        state = await self._probe_device(rec)
        if state == "mounted":
            return await self.serialize_now(rec)
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
        return await self.serialize_now(rec)

    async def unmount_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if not await self._unmount_rec(rec):
            raise BackupError("Failed to unmount backup disk")
        return await self.serialize_now(rec)

    async def scan_backup_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        return await self.serialize_now(rec)

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
        return await zfs_query.dataset_exists(dataset_name)

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
    async def _has_changes(self, base_snapshot: str, snap: str) -> bool:
        """Check if there are any file-level differences between two snapshots."""
        try:
            stdout, _stderr, rc = await run_zfs(
                "diff", base_snapshot, snap, timeout=120, check=False,
            )
            if rc != 0:
                return True
            return bool(stdout.strip())
        except Exception:
            return True

    async def start_run_backup(
        self, db: Session, dataset_name: str, backup_disk_id: int,
        backup_type: str = "full",
    ) -> BackupRun:
        """Kick off a backup run as a background task; returns the run record
        immediately so the caller sees a ``running`` entry in the UI."""
        if backup_type not in ("full", "incremental"):
            raise ValidationError("backup_type must be 'full' or 'incremental'")
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
            status="running",
            phase="pending",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        task = asyncio.create_task(
            self._run_backup_worker(run.id),
        )
        self._backup_tasks.add(task)
        task.add_done_callback(self._backup_tasks.discard)
        return run

    async def _run_backup_worker(self, run_id: int) -> None:
        """Background task that performs the actual backup."""
        try:
            from ..database import get_db_context
            with get_db_context() as db:
                run = db.query(BackupRun).filter(BackupRun.id == run_id).first()
                if not run:
                    return
                await self._do_backup(db, run)
        except Exception as e:
            logger.error("background backup failed for run %s: %s", run_id, e, exc_info=True)

    async def _do_backup(self, db: Session, run: BackupRun) -> None:
        """Core backup logic operating on an existing BackupRun record."""
        rec = db.query(BackupDisk).filter(BackupDisk.id == run.backup_disk_id).first()
        snap = None
        try:
            run.phase = "snapshotting"
            db.commit()

            await self.mount_backup_disk(db, run.backup_disk_id)

            backup_type = run.backup_type
            base_snapshot = None
            full_anchor = None
            if backup_type == "incremental":
                # Resolve the anchor BEFORE creating the new snapshot: the
                # anchor is the most recent backup-* snapshot, which would
                # otherwise be the snapshot we are about to create (choosing it
                # as the -i base makes `zfs send` reject "incremental source is
                # not earlier than it").
                base_snapshot = await self._find_anchor(run.dataset_name)
                if base_snapshot is None:
                    backup_type = "full"
                    run.backup_type = "full"
                else:
                    full_anchor = await self._find_full_anchor(run.dataset_name, base_snapshot)

            snap = f"{run.dataset_name}@{BACKUP_SNAP_PREFIX}{_ts()}"
            await run_zfs("snapshot", "-r", snap, timeout=120, check=True)
            run.snapshot = snap
            db.commit()

            # Zero-change detection for incremental backups.
            if backup_type == "incremental" and base_snapshot:
                if not await self._has_changes(base_snapshot, snap):
                    run.status = "skipped"
                    run.error = None
                    run.completed_at = datetime.now(timezone.utc)
                    db.commit()
                    await run_zfs("destroy", "-r", snap, timeout=60, check=False)
                    return

            dest_dir = self._dataset_dir(rec.mount_point, run.dataset_name)
            dest_dir.mkdir(parents=True, exist_ok=True)
            suffix = "zfs.gz"
            if backup_type == "full":
                file_name = f"full-{self._snap_ts(snap)}.{suffix}"
                send_cmd = ["zfs", "send", "-R", snap]
            else:
                file_name = f"incr-{self._snap_ts(snap)}.{suffix}"
                send_cmd = ["zfs", "send", "-R", "-i", base_snapshot, snap]
            stream_file = str(dest_dir / file_name)

            if backup_type == "full":
                needed = await self.estimate_needed(run.dataset_name)
                run.changed_bytes = 0
            else:
                needed = await self.estimate_incremental_size(db, run.dataset_name)
            if not await self.check_capacity(db, run.backup_disk_id, needed):
                await run_zfs("destroy", "-r", snap, timeout=60, check=False)
                run.status = "failed"
                run.error = "Insufficient free space on backup disk"
                db.commit()
                logger.error("backup run %s failed: %s", run.id, run.error)
                return

            run.phase = "sending"
            run.stream_file = stream_file
            db.commit()

            gzip_level = int(self.settings.backup_gzip_level)
            pipeline_task = asyncio.create_task(run_pipeline(
                [send_cmd, ["gzip", f"-{gzip_level}"]],
                stdout_path=stream_file,
                timeout=86400, check=False, op="write", category="zfs",
            ))
            # Monitor the stream file while the pipeline writes.
            while not pipeline_task.done():
                await asyncio.sleep(3)
                try:
                    run.size_bytes = Path(stream_file).stat().st_size
                    db.commit()
                except OSError:
                    pass
            _, stderr, rc = pipeline_task.result()

            if rc != 0:
                Path(stream_file).unlink(missing_ok=True)
                await run_zfs("destroy", "-r", snap, timeout=60, check=False)
                run.status = "failed"
                run.error = stderr or "zfs send failed"
                db.commit()
                logger.error("backup run %s failed: %s", run.id, run.error)
                return

            run.size_bytes = Path(stream_file).stat().st_size if Path(stream_file).exists() else 0
            if Path(stream_file).exists():
                run.sha256 = await asyncio.to_thread(bm.sha256_file, stream_file)
            run.base_snapshot = base_snapshot
            run.full_anchor = full_anchor
            if backup_type == "incremental":
                run.changed_bytes = run.size_bytes
            run.status = "success"
            run.phase = None
            run.completed_at = datetime.now(timezone.utc)
            db.commit()

            run.phase = "pruning"
            db.commit()
            if backup_type == "incremental" and base_snapshot:
                await self._prune_old_anchors(run.dataset_name, snap)
            else:
                await self._prune_old_anchors(run.dataset_name, snap, keep_full=snap)

            # Persist self-describing metadata and snapshot the config on this
            # volume so the disk can rebuild the whole system on its own.
            run.phase = None
            db.commit()
            await self._record_manifest(db, run, rec)
            if self.backup is not None:
                try:
                    await self.backup.capture_config_bundle(
                        db, rec.mount_point, media=self._media_identity(rec),
                    )
                except Exception as e:
                    logger.warning("config capture on volume failed: %s", e)
            return

        except Exception as e:
            if snap and not run.snapshot:
                await run_zfs("destroy", "-r", snap, timeout=60, check=False)
            run.status = "failed"
            run.error = str(e)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.error("backup run %s failed: %s", run.id, run.error, exc_info=True)
            return
        finally:
            await self._restore_idle_state(rec)

    async def run_backup(
        self,
        db: Session,
        dataset_name: str,
        backup_disk_id: int,
        backup_type: str = "full",
    ) -> BackupRun:
        """Run a full or incremental backup of a dataset to a backup disk (blocking)."""
        if backup_type not in ("full", "incremental"):
            raise ValidationError("backup_type must be 'full' or 'incremental'")
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
            status="running",
            phase="pending",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        await self._do_backup(db, run)
        return run

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

    # ── Backup manifest ─────────────────────────────────────────────────
    def _media_identity(self, rec: BackupDisk) -> Dict[str, Any]:
        disk = rec.disk
        return bm.media_identity(
            fs_uuid=rec.fs_uuid, label=rec.label,
            by_id=disk.by_id if disk else None,
            serial=disk.serial if disk else None,
            size_bytes=disk.size_bytes if disk else None,
            mount_point=rec.mount_point,
        )

    def _relative_stream(self, rec: BackupDisk, stream_file: str) -> str:
        try:
            return str(Path(stream_file).relative_to(rec.mount_point))
        except (ValueError, TypeError):
            return stream_file

    def _write_initial_manifest(self, rec: BackupDisk) -> None:
        """Seed an empty manifest on a freshly declared volume."""
        try:
            manifest = bm.scan_volume(
                rec.mount_point, media=self._media_identity(rec),
                nazman_version=self.settings.app_version,
            )
            bm.save_manifest(rec.mount_point, manifest)
        except Exception as e:
            logger.warning("failed to write initial manifest on %s: %s", rec.mount_point, e)

    async def _record_manifest(self, db: Session, run: BackupRun, rec: BackupDisk) -> None:
        """Write the per-stream sidecar and update the volume's aggregate manifest."""
        try:
            manifest = bm.scan_volume(
                rec.mount_point, media=self._media_identity(rec),
                nazman_version=self.settings.app_version,
            )
            if self.zfs is not None:
                try:
                    bm.merge_pools(manifest, await self.zfs.get_pool_recreate_specs(db))
                except Exception:
                    pass
                try:
                    dataset = await self.zfs.get_dataset_spec(run.dataset_name)
                except Exception:
                    dataset = {
                        "name": run.dataset_name,
                        "pool": run.dataset_name.split("/", 1)[0],
                        "properties": {},
                    }
            else:
                dataset = {
                    "name": run.dataset_name,
                    "pool": run.dataset_name.split("/", 1)[0],
                    "properties": {},
                }

            run_entry = {
                "type": run.backup_type,
                "stream_file": self._relative_stream(rec, run.stream_file or ""),
                "snapshot": run.snapshot,
                "base_snapshot": run.base_snapshot,
                "full_anchor": run.full_anchor,
                "size_bytes": run.size_bytes,
                "sha256": run.sha256,
                "created_at": (run.completed_at or datetime.now(timezone.utc)).isoformat(),
                "media_fs_uuid": rec.fs_uuid,
                "media_label": rec.label,
            }
            bm.upsert_dataset_backup(manifest, dataset, run_entry)
            bm.save_manifest(rec.mount_point, manifest)
            if run.stream_file:
                bm.write_sidecar(run.stream_file, {
                    "kind": "dataset", "dataset": dataset, "run": run_entry,
                })
        except Exception as e:
            logger.warning("failed to update backup manifest for run %s: %s", run.id, e)

    async def get_volume_manifest(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        """Read (or reconstruct) a volume's aggregate manifest."""
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise BackupDiskNotFoundError("Backup disk not found")
        was_mounted = Path(rec.mount_point).is_mount()
        if not was_mounted:
            await self.mount_backup_disk(db, backup_disk_id)
        try:
            manifest = bm.load_manifest(rec.mount_point)
            if manifest is None:
                manifest = bm.scan_volume(
                    rec.mount_point, media=self._media_identity(rec),
                    nazman_version=self.settings.app_version,
                )
            return manifest
        finally:
            if not was_mounted:
                await self._restore_idle_state(rec)

    async def rebuild_manifest(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        """Regenerate a volume's manifest by scanning streams and sidecars."""
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise BackupDiskNotFoundError("Backup disk not found")
        if not Path(rec.mount_point).is_mount():
            await self.mount_backup_disk(db, backup_disk_id)
        try:
            manifest = bm.build_from_sidecars(
                rec.mount_point, media=self._media_identity(rec),
                nazman_version=self.settings.app_version,
            )
            if self.zfs is not None:
                try:
                    bm.merge_pools(manifest, await self.zfs.get_pool_recreate_specs(db))
                except Exception:
                    pass
            bm.save_manifest(rec.mount_point, manifest)
            return manifest
        finally:
            await self._restore_idle_state(rec)

    # -- schedule synchronization ---------------------------------------------
    async def sync_scheduled_tasks(self, db: Session) -> None:
        """Reconcile backup_schedules rows into ScheduledTask (ZFS_BACKUP) jobs.

        Called on scheduler startup so scheduled full/incremental backups survive
        restarts, and used by the API when a schedule is saved or removed.
        """
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
                await self.scheduler.create_task(
                    db, name=name, task_type=TaskType.ZFS_BACKUP,
                    target=str(cfg["dataset_name"]), schedule=sched_cron, config=config,
                )
            else:
                if task.schedule != sched_cron or task.config != config:
                    await self.scheduler.update_task(
                        db, task.id, schedule=sched_cron, config=config,
                    )

        # Remove tasks whose schedule row is gone or disabled.
        for name, task in existing.items():
            if name not in desired:
                await self.scheduler.delete_task(db, task.id)

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

        try:
            return await self.receive_stream(str(fp), target_dataset, force=force)
        finally:
            await self._restore_idle_state(owner)

    async def receive_stream(
        self, stream_file: str, target_dataset: str, force: bool = False
    ) -> Dict[str, Any]:
        """Replay a gzip-compressed ZFS send stream into ``target_dataset``.

        Owner-agnostic: the caller is responsible for mounting the media and
        cleaning up idle state, so this also serves restores on a fresh install
        where no ``BackupDisk`` row exists.
        """
        validate_dataset_name(target_dataset)
        fp = Path(stream_file)
        if not fp.exists():
            raise BackupError(f"Stream file not found: {stream_file}")

        receive_cmd = ["zfs", "receive"]
        if force:
            receive_cmd.append("-F")
        receive_cmd.append(target_dataset)

        _, stderr, rc = await run_pipeline(
            [["gunzip", "-c", str(fp)], receive_cmd],
            timeout=86400, check=False, op="write", category="zfs",
        )
        if rc != 0:
            raise BackupError(f"Restore failed: {stderr}")
        return {"dataset": target_dataset, "source": str(fp), "force": force}

    # ── Router-facing aggregation (was duplicated inside api/zfs_backup) ──

    async def list_schedules(self, db: Session) -> List[Dict[str, Any]]:
        """All backup schedule rows as plain dicts."""
        return [
            {
                "id": s.id, "dataset_name": s.dataset_name,
                "backup_disk_id": s.backup_disk_id,
                "full_cron": s.full_cron, "incremental_cron": s.incremental_cron,
                "full_retention": s.full_retention,
                "incremental_retention": s.incremental_retention,
                "enabled": s.enabled,
            }
            for s in db.query(BackupSchedule).all()
        ]

    async def used_backup_targets(self, db: Session) -> List[Dict[str, Any]]:
        """(disk_id, slot_uuid) pairs unavailable as backup targets.

        Includes already-declared backup disks, in-flight declarations, and
        every device currently held by an imported pool (whole disks and
        partition members). slot_uuid is None when the whole disk is held.
        """
        used: List[Dict[str, Any]] = []
        seen = set()

        def add(disk_id: int, slot_uuid: Optional[str]) -> None:
            key = (disk_id, slot_uuid)
            if key not in seen:
                seen.add(key)
                used.append({"disk_id": disk_id, "slot_uuid": slot_uuid})

        for d in db.query(BackupDisk).all():
            add(d.disk_id, d.slot_uuid)
        for disk_id in self.pending_disk_ids():
            add(disk_id, None)
        for dev in await self._get_pool_members():
            entry = self._usage_entry_for_device(db, dev)
            if entry:
                add(entry["disk_id"], entry["slot_uuid"])
        return used

    def _usage_entry_for_device(self, db: Session, dev: str) -> Optional[Dict[str, Any]]:
        """Resolve a zpool-reported device to the disk it occupies.

        A device held by a pool (whole disk, or a partition on a disk) claims
        the whole disk: ZFS owns the disk, so none of its partitions may be
        offered as a backup target.  Always returns slot_uuid=None.
        """
        disk = None
        if dev.startswith("/dev/disk/by-id/"):
            m = re.search(r"-part(\d+)$", dev)
            base = dev[:m.start()] if m else dev
            disk = db.query(Disk).filter(Disk.by_id == base).first()
        else:
            # by-id basename (zpool drops the /dev/disk/by-id/ prefix, and for
            # partitioned vdevs also the -partN suffix); otherwise a kernel name.
            disk = db.query(Disk).filter(Disk.by_id == f"/dev/disk/by-id/{dev}").first()
            if not disk:
                disk = self._find_disk_by_device_path(db, f"/dev/{dev}")
            if not disk and dev:
                base = kernel_base_name(dev)
                if base != dev:
                    disk = self._find_disk_by_device_path(db, f"/dev/{base}")
        if not disk:
            return None
        return {"disk_id": disk.id, "slot_uuid": None}

    @staticmethod
    def _find_disk_by_device_path(db: Session, dev_path: str) -> Optional[Disk]:
        for d in db.query(Disk).all():
            if get_device_path(d) == dev_path:
                return d
        return None

    async def update_backup_disk(
        self, db: Session, backup_disk_id: int, unmount_after_backup: Optional[bool] = None
    ) -> Dict[str, Any]:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise BackupDiskNotFoundError("Backup disk not found")
        if unmount_after_backup is not None:
            rec.unmount_after_backup = unmount_after_backup
        db.commit()
        db.refresh(rec)
        return await self.serialize_now(rec)

    async def list_runs(self, db: Session, limit: int = 200) -> List[BackupRun]:
        return db.query(BackupRun).order_by(BackupRun.id.desc()).limit(limit).all()

    def get_run(self, db: Session, run_id: int) -> BackupRun:
        run = db.query(BackupRun).filter(BackupRun.id == run_id).first()
        if not run:
            raise BackupRunNotFoundError("Run not found")
        return run

    async def list_backupable_datasets(self, db: Session) -> List[Dict[str, Any]]:
        """All datasets with per-disk schedules, backup status, and run info.

        Datasets are enumerated live from ZFS; there is no DB table of datasets.
        """
        dataset_names = await zfs_query.all_filesystem_names()

        disk_labels = {d.id: d.label for d in db.query(BackupDisk).all()}
        schedules_by_dataset: Dict[str, List[BackupSchedule]] = {}
        for s in db.query(BackupSchedule).all():
            schedules_by_dataset.setdefault(s.dataset_name, []).append(s)

        runs = db.query(BackupRun).order_by(BackupRun.id.desc()).all()
        changed_since_full: Dict[str, int] = {}
        full_runs: Dict[str, int] = {}
        last_run: Dict[str, BackupRun] = {}
        last_run_per_disk: Dict[tuple, BackupRun] = {}
        full_seen = set()
        for r in runs:
            last_run.setdefault(r.dataset_name, r)
            last_run_per_disk.setdefault((r.dataset_name, r.backup_disk_id), r)
            if r.status != "success":
                continue
            full_runs[r.dataset_name] = full_runs.get(r.dataset_name, 0) + 1
            if r.backup_type == "full":
                changed_since_full[r.dataset_name] = 0
                full_seen.add(r.dataset_name)
            elif r.dataset_name not in full_seen:
                # Sum of incremental streams after the most recent full backup.
                changed_since_full[r.dataset_name] = (
                    changed_since_full.get(r.dataset_name, 0) + (r.changed_bytes or 0)
                )

        out = []
        for name in dataset_names:
            scheds = sorted(schedules_by_dataset.get(name, []), key=lambda s: s.backup_disk_id)
            last = last_run.get(name)
            out.append({
                "name": name,
                "schedules": [
                    {
                        "backup_disk_id": s.backup_disk_id,
                        "label": disk_labels.get(s.backup_disk_id) or f"Disk {s.backup_disk_id}",
                        "full_cron": s.full_cron,
                        "incremental_cron": s.incremental_cron,
                        "enabled": s.enabled,
                        "last_type": last_run_per_disk.get((name, s.backup_disk_id)).backup_type
                        if last_run_per_disk.get((name, s.backup_disk_id)) else None,
                        "last_status": last_run_per_disk.get((name, s.backup_disk_id)).status
                        if last_run_per_disk.get((name, s.backup_disk_id)) else None,
                    }
                    for s in scheds
                ],
                "full_cron": scheds[0].full_cron if scheds else None,
                "incremental_cron": scheds[0].incremental_cron if scheds else None,
                "enabled": bool(scheds),
                "last_type": last.backup_type if last else None,
                "last_status": last.status if last else None,
                "last_changed_bytes": last.changed_bytes if last else 0,
                "last_completed_at": (last.completed_at or last.started_at) if last else None,
                "changed_since_full": changed_since_full.get(name, 0),
                "full_runs": full_runs.get(name, 0),
            })
        return out

    async def upsert_schedule(self, db: Session, body: Dict[str, Any]) -> BackupSchedule:
        """Create or update the backup schedule for a (dataset, disk) pair."""
        dataset_name = body.get("dataset_name")
        backup_disk_id = body.get("backup_disk_id")
        if not dataset_name:
            raise ValidationError("dataset_name required")
        if not backup_disk_id:
            raise ValidationError("backup_disk_id required")
        sched = db.query(BackupSchedule).filter(
            BackupSchedule.dataset_name == dataset_name,
            BackupSchedule.backup_disk_id == backup_disk_id,
        ).first()
        if not sched:
            sched = BackupSchedule(dataset_name=dataset_name, backup_disk_id=backup_disk_id)
            db.add(sched)
        sched.full_cron = body.get("full_cron")
        sched.incremental_cron = body.get("incremental_cron")
        sched.full_retention = body.get("full_retention", 3)
        sched.incremental_retention = body.get("incremental_retention", 7)
        sched.enabled = body.get("enabled", True)
        db.commit()
        db.refresh(sched)

        # Reconcile ScheduledTask jobs so saved crons actually fire.
        await self.sync_scheduled_tasks(db)
        return sched

    async def delete_schedules(
        self, db: Session, dataset_name: str, backup_disk_id: Optional[int] = None
    ) -> List[int]:
        """Remove the dataset's schedule on one disk (or all disks when no disk given)."""
        query = db.query(BackupSchedule).filter(BackupSchedule.dataset_name == dataset_name)
        if backup_disk_id is not None:
            query = query.filter(BackupSchedule.backup_disk_id == backup_disk_id)
        removed = []
        for sched in query.all():
            removed.append(sched.backup_disk_id)
            db.delete(sched)
        db.commit()
        await self.sync_scheduled_tasks(db)
        return removed


