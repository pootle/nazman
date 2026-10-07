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
from ..utils.paths import ensure_dir
from ..models.disk import Disk
from ..models.backup_zfs import (
    BackupDisk, BackupGroup, BackupRun, BackupSet,
)

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
    """Backup ZFS datasets to a declared, formatted disk.

    Backups are ZFS snapshot streams written to files (compact stream format):
      full:  zfs send -c -R <ds>@backup-<ts> | tee full-<ts>.zfs.gz | sha256sum
      incr:  zfs send -c -R -i <ds>@backup-<prev> <ds>@backup-<ts> | tee incr-<ts>.zfs.gz | sha256sum

    ``zfs send -c`` ships each block in whatever form the pool stored it, so
    data ZFS already compressed (compression=zstd/lz4) travels compressed with
    no extra pass and no backup-time CPU.  Files keep the legacy ``*.zfs.gz``
    suffix so paths, manifests and restore tooling stay unchanged; restore
    detects legacy gzip files by their magic bytes.

    This manager owns one dataset's write: mount the disk, resolve the
    incremental anchor, snapshot, check space, send, prune and record the
    manifest.  Deciding *which* dataset is written to *which* disk is the
    backup group's job (``services/backup_group_service.py``), which drives
    this through :meth:`backup_dataset`.

    Incremental backups require the base ``backup-*`` snapshot to still exist
    on the source.  The anchor is the last snapshot the dataset's *backup set*
    successfully received, so a set is a self-consistent chain even though the
    base may sit on an earlier disk of the same set; if that snapshot has been
    pruned the write is promoted to a full.
    """

    def __init__(self, zfs=None, backup=None):
        self.settings = get_settings()
        # Collaborators injected at wiring time (constructor injection keeps
        # the manager independently testable and free of import cycles).
        self.zfs = zfs
        self.backup = backup
        self._pending_declares: Dict[int, Dict[str, Any]] = {}
        self._declare_tasks: set = set()

    # ── Backup disk declaration / formatting ─────────────────────────────
    async def get_mount_base(self) -> Path:
        return ensure_dir(Path(self.settings.backup_mount_base))

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
            "backup_set_id": rec.backup_set_id,
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

    @staticmethod
    def _declare_mark(t0: float, prev: float, disk_id: int, step: str) -> float:
        """Log how long one declaration step took, returning the new wall clock."""
        now = time.monotonic()
        logger.info("declare disk %s: %s took %.1fs (total %.1fs)",
                    disk_id, step, now - prev, now - t0)
        return now

    async def start_declare_backup_disk(
        self, db: Session, disk_id: int, confirm: bool = False,
        slot_uuid: Optional[str] = None, label: Optional[str] = None,
        wipe_raid: bool = False, backup_set_id: Optional[int] = None,
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
            "backup_set_id": backup_set_id,
            "error": None,
            "started_at": datetime.now(timezone.utc),
        }
        self._pending_declares[disk_id] = entry
        task = asyncio.create_task(
            self._run_declare(disk_id, slot_uuid, label, wipe_raid, backup_set_id)
        )
        self._declare_tasks.add(task)
        task.add_done_callback(self._declare_tasks.discard)
        return self._pending_to_dict(entry)

    async def _run_declare(
        self, disk_id: int, slot_uuid: Optional[str], label: Optional[str],
        wipe_raid: bool, backup_set_id: Optional[int] = None,
    ) -> None:
        """Background task executing the actual wipe+format declaration."""
        t0 = time.monotonic()
        try:
            from ..database import get_db_context
            with get_db_context() as db:
                await self.declare_backup_disk(
                    db, disk_id, confirm=True,
                    slot_uuid=slot_uuid, label=label, wipe_raid=wipe_raid,
                    backup_set_id=backup_set_id,
                )
            self._pending_declares.pop(disk_id, None)
            logger.info("declare disk %s finished in %.1fs", disk_id, time.monotonic() - t0)
        except Exception as e:
            entry = self._pending_declares.get(disk_id)
            if entry:
                entry["status"] = "failed"
                entry["error"] = str(e)
            logger.error("background declare failed for disk %s after %.1fs: %s",
                         disk_id, time.monotonic() - t0, e, exc_info=True)

    async def declare_backup_disk(
        self, db: Session, disk_id: int, confirm: bool = False,
        slot_uuid: Optional[str] = None, label: Optional[str] = None,
        wipe_raid: bool = False, backup_set_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Declare a disk or partition as a backup target: validate, format, mount.

        ``confirm`` mirrors the destructive-action guard used elsewhere in the
        UI (the caller must send confirm=True to allow the wipe+format).
        With ``slot_uuid`` the named ZFS-style partition is formatted in place
        (its GPT PARTLABEL survives); without it the whole disk is wiped to a
        single ext4 partition.  ``wipe_raid`` authorises stopping software RAID
        arrays and zeroing their superblocks on the target device(s).
        ``backup_set_id`` files the freshly formatted volume into a backup set,
        where it becomes that set's active disk if the set has none.
        """
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise ValidationError("Disk not found")
        if disk.is_os_disk:
            raise ValidationError("Cannot use the OS disk as a backup disk")
        if not confirm:
            raise ValidationError("Destructive action requires confirmation")

        t0 = prev = time.monotonic()
        pool_members = await self._get_pool_members()
        member_pool = zfs_query.pool_member_for_disk(pool_members, disk)
        if member_pool:
            raise ValidationError(f"Disk is a member of pool '{member_pool}'; remove it from the pool first")
        prev = self._declare_mark(t0, prev, disk_id, "pool check")

        if db.query(BackupDisk).filter(BackupDisk.disk_id == disk_id).first():
            raise ValidationError("Disk is already declared as a backup disk")

        if slot_uuid:
            part_dev = await self._resolve_partition(disk, slot_uuid)
            member_pool = self._pool_member_for_device(pool_members, part_dev)
            if member_pool:
                raise ValidationError(f"Partition is a member of pool '{member_pool}'; remove it from the pool first")
            await self._handle_raid([part_dev], wipe_raid)
            prev = self._declare_mark(t0, prev, disk_id, "resolve + raid check")
            await self._ensure_unused(part_dev)
            prev = self._declare_mark(t0, prev, disk_id, "unused check")
            # Format only the partition; keep the GPT so the slot UUID survives.
            await self._run_destructive(["wipefs", "-a", part_dev], 120, "wipe the partition")
            prev = self._declare_mark(t0, prev, disk_id, "wipe signatures")
            await self._run_destructive(["mkfs.ext4", "-F", part_dev], 600, "format the partition")
            prev = self._declare_mark(t0, prev, disk_id, "format")
            device_path = part_dev
            partition_number = self._partition_number(part_dev)
        else:
            dev = get_device_path(disk)
            if not dev:
                raise ValidationError("Disk is not currently present")

            devices = [dev] + await self._disk_partition_paths(dev)
            await self._handle_raid(devices, wipe_raid)
            await self._ensure_unused(dev)
            prev = self._declare_mark(t0, prev, disk_id, "raid + unused check")
            # Wipe and create a single GPT partition covering the whole disk.
            await self._run_destructive(["wipefs", "-a", dev], 120, "wipe existing signatures")
            prev = self._declare_mark(t0, prev, disk_id, "wipe signatures")
            await self._run_destructive(["parted", "-s", dev, "mklabel", "gpt"], 120, "create the GPT partition table")
            prev = self._declare_mark(t0, prev, disk_id, "create GPT")
            await self._run_destructive(["parted", "-s", dev, "mkpart", "primary", "0%", "100%"], 120, "create the partition")
            prev = self._declare_mark(t0, prev, disk_id, "create partition")
            # Let the kernel see the new partition.
            await self._run_destructive(["partprobe", dev], 120, "rescan the partition table")
            prev = self._declare_mark(t0, prev, disk_id, "rescan partition table")

            device_path = self._whole_partition_device(disk, dev)
            await self._run_destructive(["mkfs.ext4", "-F", device_path], 600, "format the partition")
            prev = self._declare_mark(t0, prev, disk_id, "format")
            partition_number = 1

        # Read back the filesystem UUID for deterministic remounting.
        fs_uuid = await self._fs_uuid(device_path)
        prev = self._declare_mark(t0, prev, disk_id, "read UUID")
        if not fs_uuid:
            raise BackupError("Could not read filesystem UUID after formatting")

        mount_base = await self.get_mount_base()
        mount_point = str(ensure_dir(mount_base / fs_uuid))
        await run_command(["mount", device_path, mount_point], timeout=60, check=False, op="write", category="disk")
        prev = self._declare_mark(t0, prev, disk_id, "mount")

        rec = BackupDisk(
            disk_id=disk_id,
            slot_uuid=slot_uuid,
            partition_number=partition_number,
            label=label,
            fs_type="ext4",
            mount_point=mount_point,
            fs_uuid=fs_uuid,
            backup_set_id=backup_set_id,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        if backup_set_id is not None:
            await self._adopt_into_set(db, backup_set_id, rec)
        await self._seed_volume(db, rec)
        prev = self._declare_mark(t0, prev, disk_id, "seed volume")
        if rec.unmount_after_backup:
            await self._unmount_rec(rec)
            prev = self._declare_mark(t0, prev, disk_id, "unmount")
        logger.info("declare disk %s complete in %.1fs", disk_id, time.monotonic() - t0)
        return await self.serialize_now(rec)

    async def _adopt_into_set(self, db: Session, set_id: int, rec: BackupDisk) -> None:
        """File a backup disk into a set, activating it if the set has none.

        Set membership and the active-disk pointer form a cycle, so this is
        maintained in one place on both sides of it rather than by a constraint.
        """
        bset = db.query(BackupSet).filter(BackupSet.id == set_id).first()
        if not bset:
            raise ValidationError("Backup set not found")
        rec.backup_set_id = bset.id
        if bset.active_disk_id is None:
            bset.active_disk_id = rec.id
        db.commit()

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
        """What a wipe of this disk/partition would destroy (for the UI probe).

        ``md`` is the software RAID superblock list; ``partitions`` classifies
        each affected partition as a backup volume, a ZFS pool member, or a
        foreign signature (md/LVM/swap/LUKS) so the confirm dialog can say
        exactly what the wipe takes.
        """
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise ValidationError("Disk not found")
        pool_members = await self._get_pool_members()
        if slot_uuid:
            dev = await self._resolve_partition(disk, slot_uuid)
            devices = [dev]
        else:
            dev = get_device_path(disk)
            if not dev:
                raise ValidationError("Disk is not currently present")
            devices = [dev] + await self._disk_partition_paths(dev)
        md = await self._md_superblocks(devices)
        partitions = await self._target_partitions(
            db, disk, dev, pool_members, md
        )
        return {"device": dev, "md": md, "partitions": partitions}

    async def _target_partitions(
        self, db: Session, disk: Disk, dev: str,
        pool_members: Dict[str, str], md: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Classify every partition a wipe of ``dev`` would destroy.

        Partitions this app tracks get a detail naming the backup set/group or
        ZFS pool; everything else is labelled by its filesystem signature so
        the user can see it is a foreign RAID/LVM/swap/etc. member.
        """
        stdout, _, rc = await run_command(
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,FSTYPE,UUID,PARTLABEL,PARTUUID", dev],
            timeout=30, check=False, op="read", category="disk",
        )
        nodes: List[Dict[str, Any]] = []
        if rc == 0 and stdout.strip():
            try:
                data = json.loads(stdout)

                def walk(devs: List[Dict[str, Any]]) -> None:
                    for node in devs:
                        if node.get("type") == "part":
                            nodes.append(node)
                        walk(node.get("children", []))

                walk(data.get("blockdevices", []))
            except ValueError:
                pass

        backup_rec = db.query(BackupDisk).filter(BackupDisk.disk_id == disk.id).first()
        datasets: List[str] = []
        if backup_rec:
            datasets = [r[0] for r in db.query(BackupRun.dataset_name).filter(
                BackupRun.backup_disk_id == backup_rec.id,
                BackupRun.status == "success",
            ).distinct()]

        md_by_name = {Path(m.get("device", "")).name: m for m in md}
        os_names = await os_reserved_partition_names()

        out: List[Dict[str, Any]] = []
        for node in nodes:
            name = node.get("name", "")
            device_path = f"/dev/{name}"
            partlabel = node.get("partlabel") or ""
            if partlabel.startswith("nazman:"):
                slot = partlabel[len("nazman:"):]
            else:
                slot = node.get("partuuid") or None
            part_num = self._partition_number(device_path)
            fstype = node.get("fstype") or ""
            entry = {
                "number": part_num,
                "device_path": device_path,
                "slot_uuid": slot,
                "kind": "unknown",
                "detail": f"{fstype} filesystem" if fstype else "no filesystem",
            }

            if backup_rec:
                matched = bool(
                    (backup_rec.slot_uuid and slot and backup_rec.slot_uuid == slot)
                    or (not backup_rec.slot_uuid and backup_rec.partition_number == part_num)
                )
                if not matched and node.get("uuid") and node.get("uuid") == backup_rec.fs_uuid:
                    matched = True
                if matched:
                    detail = "declared backup volume"
                    if backup_rec.backup_set and backup_rec.backup_set.group:
                        bset = backup_rec.backup_set
                        set_name = bset.label or f"set {bset.position + 1}"
                        detail += f" of set '{set_name}'"
                        detail += f" in group '{bset.group.name}'"
                    else:
                        detail += " (not in a backup set)"
                    if datasets:
                        detail += "; holds " + ", ".join(datasets)
                    entry.update({"kind": "backup_disk", "detail": detail})
                    out.append(entry)
                    continue

            pool = self._pool_member_for_device(pool_members, device_path)
            if pool:
                entry.update({"kind": "zfs_pool", "detail": f"member of ZFS pool '{pool}'"})
                out.append(entry)
                continue

            sb = md_by_name.get(name)
            if sb or fstype == "linux_raid_member":
                if sb:
                    detail = "software RAID array "
                    detail += f"'{sb.get('name') or 'unknown'}"
                    detail += f"' (v{sb.get('version') or '?'})"
                else:
                    detail = "software RAID member (linux_raid_member)"
                entry.update({"kind": "md_raid", "detail": detail})
                out.append(entry)
                continue

            foreign = {
                "LVM2_member": ("lvm", "LVM physical volume"),
                "swap": ("swap", "swap space"),
                "crypto_LUKS": ("luks", "LUKS encrypted volume"),
            }
            if fstype in foreign:
                kind, detail = foreign[fstype]
                entry.update({"kind": kind, "detail": detail})
                out.append(entry)
                continue

            if name in os_names:
                entry.update({"kind": "os", "detail": "OS/boot reserved partition"})
                out.append(entry)
                continue

            out.append(entry)
        return out

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
        ensure_dir(rec.mount_point)
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
        """Undeclare a backup disk, keeping the data on the medium.

        The runs for this disk reference stream files stored on it, so with the
        disk deregistered those records are meaningless and go too (the FK
        cascade also covers this when foreign_keys is enabled).  The owning
        set's active-disk pointer and its group's needs-disk flag are realigned
        so the group can keep rotating.
        """
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if Path(rec.mount_point).is_mount():
            try:
                await run_command(["umount", rec.mount_point], timeout=60, check=False, op="write", category="disk")
            except Exception:
                pass
        db.query(BackupRun).filter(BackupRun.backup_disk_id == backup_disk_id).delete()
        bset = self._set_of(db, rec)
        if bset is not None and bset.active_disk_id == rec.id:
            remaining = (
                db.query(BackupDisk)
                .filter(BackupDisk.backup_set_id == bset.id, BackupDisk.id != rec.id)
                .order_by(BackupDisk.id)
                .all()
            )
            bset.active_disk_id = remaining[0].id if remaining else None
            group = bset.group
            if group is not None and not remaining:
                group.needs_disk = True
        db.delete(rec)
        db.commit()

    async def recycle_disk(self, db: Session, backup_disk_id: int) -> Dict[str, Any]:
        """Wipe and reformat a backup disk so its chain restarts.

        Used when a full disk's turn comes around and the group's
        ``recycle_full_disks`` setting is on: the volume is reformatted with a
        new filesystem UUID and its stream records are deleted, so the owning
        set writes a fresh full next time.  The disk keeps its identity
        (``disks.by_id``), its partition slot, and its place in the set.
        """
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        dev = self._dev_path(rec)
        if not dev or not os.path.exists(dev):
            raise BackupError(
                f"Backup disk '{rec.label or rec.fs_uuid}' is not connected"
            )
        if not await self._unmount_rec(rec):
            raise BackupError("Could not unmount the backup disk before recycling")
        await self._run_destructive(["wipefs", "-a", dev], 120, "wipe the backup disk")
        await self._run_destructive(["mkfs.ext4", "-F", dev], 600, "format the backup disk")
        fs_uuid = await self._fs_uuid(dev)
        if not fs_uuid:
            raise BackupError("Could not read filesystem UUID after recycling")
        mount_base = await self.get_mount_base()
        rec.fs_uuid = fs_uuid
        rec.mount_point = str(mount_base / fs_uuid)
        db.query(BackupRun).filter(BackupRun.backup_disk_id == rec.id).delete()
        db.commit()
        return await self.serialize_now(rec)

    @staticmethod
    def _set_of(db: Session, rec: BackupDisk) -> Optional[BackupSet]:
        if not rec.backup_set_id:
            return None
        return db.query(BackupSet).filter(BackupSet.id == rec.backup_set_id).first()

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

    async def estimate_incremental_size(
        self, db: Session, dataset_name: str, backup_set_id: Optional[int] = None,
    ) -> int:
        """Estimate an incremental's size: the set's last incremental's
        ``changed_bytes``, else 10% of used.  Scoped to the set so the estimate
        reflects the chain that is actually about to be extended."""
        query = db.query(BackupRun).filter(
            BackupRun.dataset_name == dataset_name,
            BackupRun.backup_type == "incremental",
            BackupRun.status == "success",
        )
        if backup_set_id is not None:
            query = query.filter(BackupRun.backup_set_id == backup_set_id)
        last = query.order_by(BackupRun.id.desc()).first()
        if last and last.changed_bytes:
            return last.changed_bytes
        used = await self.estimate_full_size(dataset_name)
        return int(used * 0.1) if used else 0

    async def estimate_needed(self, dataset_name: str) -> int:
        """Needed bytes for a full backup of a dataset (with safety margin)."""
        used = await self.estimate_full_size(dataset_name)
        return int(used * self.settings.backup_full_margin) if used else 0

    @staticmethod
    def free_bytes(rec: BackupDisk) -> Optional[int]:
        """Free space on a mounted backup disk, or None when it cannot be read."""
        if not rec or not rec.mount_point:
            return None
        try:
            st = os.statvfs(rec.mount_point)
        except OSError:
            return None
        return st.f_frsize * st.f_bavail

    async def check_capacity(self, db: Session, backup_disk_id: int, needed_bytes: int) -> bool:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if not Path(rec.mount_point).is_mount():
            raise BackupError("Backup disk is not mounted; cannot check capacity")
        free = self.free_bytes(rec)
        return free is not None and free >= needed_bytes

    def fits(self, rec: BackupDisk, needed_bytes: int) -> bool:
        """Cheap pre-flight: is this disk mounted with room for ``needed_bytes``?"""
        free = self.free_bytes(rec)
        return free is not None and free >= needed_bytes

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

    async def backup_dataset(
        self, db: Session, rec: BackupDisk, run: BackupRun,
    ) -> Dict[str, Any]:
        """Write one dataset's stream to a mounted backup disk.

        The order is deliberate and matches the backup contract: mount the
        disk, resolve the anchor, snapshot, skip if nothing changed, check
        space, send, prune anchors, record the manifest.  The space check
        deliberately follows the snapshot so a dataset that is already
        unchanged never costs anything on the target.

        The caller owns the ``run`` row (the group service fills in session,
        group and set) and owns set membership; this only reports back.  The
        returned dict always has a ``status`` of ``success``, ``skipped`` or
        ``failed``, plus ``needs_space`` when the write was refused for lack
        of room - in which case the snapshot taken here is left in place for
        the retry on the next disk rather than destroyed and re-taken.
        """
        snap = None
        needs_space = False
        try:
            run.phase = "snapshotting"
            db.commit()
            await self.mount_backup_disk(db, rec.id)

            backup_type = run.backup_type
            base_snapshot = None
            full_anchor = None
            if backup_type == "incremental":
                # Resolve the anchor BEFORE creating the new snapshot: the base
                # is the newest snapshot this *set* received, which would
                # otherwise be the snapshot we are about to create (choosing it
                # as the -i base makes `zfs send` reject "incremental source is
                # not earlier than it").  With no base the set is starting a
                # fresh chain, so this is promoted to a full.
                base_snapshot = await self.set_anchor(db, run.dataset_name, run.backup_set_id)
                if base_snapshot is None:
                    run.promoted_from = "incremental"
                    backup_type = "full"
                    run.backup_type = "full"
                else:
                    full_anchor = await self.chain_full_anchor(db, run.dataset_name, run.backup_set_id)

            # A retry on another disk reuses the snapshot this refused write
            # already took, so the dataset is captured once per session and no
            # stray snapshot is left behind on the source.
            snap = run.snapshot
            if not snap or not await self._snapshot_exists(snap):
                snap = f"{run.dataset_name}@{BACKUP_SNAP_PREFIX}{_ts()}"
                await run_zfs("snapshot", "-r", snap, timeout=120, check=True)
                run.snapshot = snap
            db.commit()

            # Zero-change detection for incremental backups.
            if backup_type == "incremental" and base_snapshot:
                if not await self._has_changes(base_snapshot, snap):
                    run.status = "skipped"
                    run.error = None
                    run.base_snapshot = base_snapshot
                    run.full_anchor = full_anchor
                    run.phase = None
                    run.completed_at = datetime.now(timezone.utc)
                    db.commit()
                    await run_zfs("destroy", "-r", snap, timeout=60, check=False)
                    return {"status": "skipped", "run": run, "snapshot": snap}

            dest_dir = self._dataset_dir(rec.mount_point, run.dataset_name)
            dest_dir.mkdir(parents=True, exist_ok=True)
            if backup_type == "full":
                file_name = f"full-{self._snap_ts(snap)}.zfs.gz"
                send_cmd = ["zfs", "send", "-c", "-R", snap]
            else:
                file_name = f"incr-{self._snap_ts(snap)}.zfs.gz"
                send_cmd = ["zfs", "send", "-c", "-R", "-i", base_snapshot, snap]
            stream_file = str(dest_dir / file_name)

            if backup_type == "full":
                needed = await self.estimate_needed(run.dataset_name)
                run.changed_bytes = 0
            else:
                needed = await self.estimate_incremental_size(
                    db, run.dataset_name, run.backup_set_id,
                )
            run.estimated_bytes = needed
            if not await self.check_capacity(db, rec.id, needed):
                needs_space = True
                run.status = "failed"
                run.error = "Insufficient free space on backup disk"
                db.commit()
                logger.warning(
                    "backup of %s to disk %s needs ~%d bytes but did not fit",
                    run.dataset_name, rec.id, needed,
                )
                return {
                    "status": "needs_space", "needs_space": True, "run": run,
                    "snapshot": snap, "needed_bytes": needed,
                    "free_bytes": self.free_bytes(rec) or 0,
                }

            run.phase = "sending"
            run.stream_file = stream_file
            db.commit()

            pipeline_task = asyncio.create_task(run_pipeline(
                [send_cmd, ["tee", stream_file], ["sha256sum"]],
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
            stdout, stderr, rc = pipeline_task.result()

            if rc != 0:
                Path(stream_file).unlink(missing_ok=True)
                await self._discard_snapshot(snap)
                run.status = "failed"
                run.error = stderr or "zfs send failed"
                db.commit()
                logger.error("backup run %s failed: %s", run.id, run.error)
                return {"status": "failed", "run": run, "error": run.error}

            run.size_bytes = Path(stream_file).stat().st_size if Path(stream_file).exists() else 0
            if Path(stream_file).exists():
                run.sha256 = ((stdout or "").strip().split() or [None])[0]
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
            await self._prune_old_anchors(db, run.dataset_name, keep=snap, keep_full=snap)

            # Persist self-describing metadata so the volume can be restored
            # on its own; the config bundle is the session's job, not this
            # dataset's, so it is written once per session on the disk used.
            run.phase = None
            db.commit()
            await self._record_manifest(db, run, rec)
            return {"status": "success", "run": run, "stream_file": stream_file}

        except Exception as e:
            if snap and not run.snapshot:
                await self._discard_snapshot(snap)
            run.status = "failed"
            run.error = str(e)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.error("backup run %s failed: %s", run.id, run.error, exc_info=True)
            return {"status": "failed", "run": run, "error": run.error}
        finally:
            if not needs_space:
                await self._restore_idle_state(rec)

    async def _snapshot_exists(self, snap: str) -> bool:
        """Is this snapshot still in the pool?"""
        if not snap:
            return False
        _stdout, _stderr, rc = await run_zfs(
            "list", "-H", "-o", "name", snap, timeout=60, check=False,
        )
        return rc == 0

    async def _discard_snapshot(self, snap: str) -> None:
        """Drop a snapshot taken for a write that will not go ahead."""
        await run_zfs("destroy", "-r", snap, timeout=60, check=False)

    # -- Incremental anchoring (per backup set) ------------------------------
    async def set_anchor(
        self, db: Session, dataset_name: str, backup_set_id: Optional[int],
    ) -> Optional[str]:
        """The snapshot an incremental for this dataset should build on.

        A set is one chain spread over its disks, so the base is the snapshot
        of the newest successful run *in that set* - not the newest snapshot
        of the dataset, which may have been sent to a different set.  It must
        still exist in the pool, because ``zfs send -i`` resolves it on the
        source: if pruning removed it the caller promotes to a full.
        """
        if backup_set_id is None:
            return None
        last = (
            db.query(BackupRun)
            .filter(
                BackupRun.dataset_name == dataset_name,
                BackupRun.backup_set_id == backup_set_id,
                BackupRun.status == "success",
                BackupRun.snapshot.isnot(None),
            )
            .order_by(BackupRun.id.desc())
            .first()
        )
        if not last or not last.snapshot:
            return None
        snaps = await self._list_backup_snapshots(dataset_name)
        if last.snapshot in snaps:
            return last.snapshot
        logger.info(
            "anchor %s for %s in set %s is gone; promoting to full",
            last.snapshot, dataset_name, backup_set_id,
        )
        return None

    async def chain_full_anchor(
        self, db: Session, dataset_name: str, backup_set_id: Optional[int],
    ) -> Optional[str]:
        """The full backup this set's incremental chain derives from."""
        if backup_set_id is None:
            return None
        first = (
            db.query(BackupRun)
            .filter(
                BackupRun.dataset_name == dataset_name,
                BackupRun.backup_set_id == backup_set_id,
                BackupRun.status == "success",
                BackupRun.backup_type == "full",
            )
            .order_by(BackupRun.id.asc())
            .first()
        )
        return first.snapshot if first else None

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
            if not line or "@" not in line:
                continue
            ds, snap = line.split("@", 1)
            if ds == dataset_name and snap.startswith(BACKUP_SNAP_PREFIX):
                names.append(line)
        names.sort()
        return names

    async def _prune_old_anchors(
        self, db: Session, dataset_name: str, keep: str, keep_full: Optional[str] = None,
    ) -> None:
        """Destroy ``backup-*`` snapshots no live chain still needs.

        Anchors are shared: a set's chain can span several disks, and a sibling
        set may still be extending the same dataset's chain, so anything
        referenced by a successful run is kept.  Only the snapshot just written
        and its chain start are exempt beyond that, plus the newest pair as a
        safety buffer against a concurrent run.
        """
        snaps = await self._list_backup_snapshots(dataset_name)
        if not snaps:
            return
        referenced = set()
        for run in (
            db.query(BackupRun)
            .filter(BackupRun.dataset_name == dataset_name, BackupRun.status == "success")
            .all()
        ):
            for snap_name in (run.snapshot, run.base_snapshot, run.full_anchor):
                if snap_name:
                    referenced.add(snap_name)
        exempt = {keep, *snaps[-2:]}
        if keep_full:
            exempt.add(keep_full)
        for s in snaps:
            if s in exempt or s in referenced:
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

    def _set_identity(self, db: Session, run: BackupRun) -> Dict[str, Any]:
        """Which group/set this stream belongs to, for the manifest.

        A set's chain spans its disks, so the restore path groups volumes by
        ``set_id`` and rebuilds the chain across them; a stream written before
        groups existed carries neither key and stays a single-volume chain.
        """
        identity: Dict[str, Any] = {}
        if run.group_id is None:
            return identity
        group = db.query(BackupGroup).filter(BackupGroup.id == run.group_id).first()
        if group is not None:
            identity["group_id"] = group.id
            identity["group_name"] = group.name
        bset = None
        if run.backup_set_id is not None:
            bset = db.query(BackupSet).filter(BackupSet.id == run.backup_set_id).first()
        if bset is not None:
            identity["set_id"] = bset.id
            identity["set_label"] = bset.label
            identity["set_position"] = bset.position
        return identity

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
                **self._set_identity(db, run),
            }
            bm.upsert_dataset_backup(manifest, dataset, run_entry)
            if run.group_id:
                grp = db.query(BackupGroup).filter(
                    BackupGroup.id == run.group_id,
                ).first()
                if grp:
                    manifest["group"] = grp.name
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
        """Replay a ZFS send stream into ``target_dataset``.

        Modern streams are ``zfs send -c`` compact streams; legacy files are
        gzip'd.  The format is detected from the file's magic bytes so both
        restore cleanly.  Owner-agnostic: the caller is responsible for mounting
        the media and cleaning up idle state, so this also serves restores on a
        fresh install where no ``BackupDisk`` row exists.
        """
        validate_dataset_name(target_dataset)
        fp = Path(stream_file)
        if not fp.exists():
            raise BackupError(f"Stream file not found: {stream_file}")

        receive_cmd = ["zfs", "receive"]
        if force:
            receive_cmd.append("-F")
        receive_cmd.append(target_dataset)

        try:
            with open(fp, "rb") as fh:
                head = fh.read(2)
        except OSError as exc:
            raise BackupError(f"Cannot read stream: {exc}") from exc

        if head == b"\x1f\x8b":
            source: List[str] = ["gunzip", "-c", str(fp)]
        else:
            source = ["cat", str(fp)]
        _, stderr, rc = await run_pipeline(
            [source, receive_cmd],
            timeout=86400, check=False, op="write", category="zfs",
        )
        if rc != 0:
            raise BackupError(f"Restore failed: {stderr}")
        return {"dataset": target_dataset, "source": str(fp), "force": force}

    # ── Router-facing aggregation (was duplicated inside api/zfs_backup) ──

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

    async def list_runs(
        self, db: Session, group_id: Optional[int] = None, limit: int = 200,
    ) -> List[BackupRun]:
        query = db.query(BackupRun)
        if group_id is not None:
            query = query.filter(BackupRun.group_id == group_id)
        return query.order_by(BackupRun.id.desc()).limit(limit).all()

    def get_run(self, db: Session, run_id: int) -> BackupRun:
        run = db.query(BackupRun).filter(BackupRun.id == run_id).first()
        if not run:
            raise BackupRunNotFoundError("Run not found")
        return run



