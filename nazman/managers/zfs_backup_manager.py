from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from pathlib import Path
import re
import uuid

from sqlalchemy.orm import Session

from ..config import get_settings
from ..utils.commands import run_command, run_zfs, run_zpool, run_pipeline
from ..utils.exceptions import BackupError, ValidationError
from ..managers.disk_manager import get_device_path
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

    async def list_backup_disks(self, db: Session) -> List[BackupDisk]:
        disks = db.query(BackupDisk).order_by(BackupDisk.id).all()
        for d in disks:
            await self._refresh_free_space(d)
        db.commit()
        return disks

    async def _refresh_free_space(self, disk: BackupDisk) -> None:
        """Update total/free bytes from statvfs if the disk is mounted."""
        mp = Path(disk.mount_point)
        if mp.is_mount():
            try:
                import os
                st = os.statvfs(str(mp))
                total = st.f_frsize * st.f_blocks
                free = st.f_frsize * st.f_bavail
                disk.total_bytes = total
                disk.free_bytes = free
                disk.status = "mounted"
            except Exception:
                pass

    async def declare_backup_disk(
        self, db: Session, disk_id: int, confirm: bool = False
    ) -> BackupDisk:
        """Declare a disk as a backup target: validate, wipe, format ext4, mount.

        ``confirm`` mirrors the destructive-action guard used elsewhere in the
        UI (the caller must send confirm=True to allow the wipe+format).
        """
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            raise ValidationError("Disk not found")
        if disk.is_os_disk:
            raise ValidationError("Cannot use the OS disk as a backup disk")
        if not confirm:
            raise ValidationError("Destructive action requires confirmation")

        dev = get_device_path(disk)
        if not dev:
            raise ValidationError("Disk is not currently present")

        if db.query(BackupDisk).filter(BackupDisk.disk_id == disk_id).first():
            raise ValidationError("Disk is already declared as a backup disk")

        # Wipe and create a single GPT partition covering the whole disk.
        await run_command(["wipefs", "-a", dev], timeout=120, check=False, op="write", category="disk")
        await run_command(["parted", "-s", dev, "mklabel", "gpt"], timeout=120, check=False, op="write", category="disk")
        await run_command(["parted", "-s", dev, "mkpart", "primary", "0%", "100%"], timeout=120, check=False, op="write", category="disk")
        # Let the kernel see the new partition.
        await run_command(["partprobe", dev], timeout=120, check=False, op="write", category="disk")

        part_dev = self._whole_partition_device(disk, dev)
        await run_command(["mkfs.ext4", "-F", part_dev], timeout=600, check=False, op="write", category="disk")

        # Read back the filesystem UUID for deterministic remounting.
        fs_uuid = await self._fs_uuid(part_dev)
        if not fs_uuid:
            raise BackupError("Could not read filesystem UUID after formatting")

        mount_base = await self.get_mount_base()
        mount_point = str(mount_base / fs_uuid)
        Path(mount_point).mkdir(parents=True, exist_ok=True)
        await run_command(["mount", part_dev, mount_point], timeout=60, check=False, op="write", category="disk")

        rec = BackupDisk(
            disk_id=disk_id,
            device_path=part_dev,
            fs_type="ext4",
            mount_point=mount_point,
            fs_uuid=fs_uuid,
            status="mounted",
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        await self._refresh_free_space(rec)
        db.commit()
        return rec

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

    async def _fs_uuid(self, dev: str) -> Optional[str]:
        stdout, _, rc = await run_command(
            ["blkid", "-s", "UUID", "-o", "value", dev], timeout=30, check=False, op="read", category="disk"
        )
        return stdout.strip() or None

    async def mount_backup_disk(self, db: Session, backup_disk_id: int) -> BackupDisk:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        Path(rec.mount_point).mkdir(parents=True, exist_ok=True)
        if not Path(rec.mount_point).is_mount():
            await run_command(["mount", rec.device_path, rec.mount_point], timeout=60, check=False, op="write", category="disk")
        rec.status = "mounted"
        await self._refresh_free_space(rec)
        db.commit()
        return rec

    async def unmount_backup_disk(self, db: Session, backup_disk_id: int) -> BackupDisk:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        if Path(rec.mount_point).is_mount():
            await run_command(["umount", rec.mount_point], timeout=60, check=False, op="write", category="disk")
        rec.status = "unmounted"
        db.commit()
        return rec

    async def scan_backup_disk(self, db: Session, backup_disk_id: int) -> BackupDisk:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise ValidationError("Backup disk not found")
        await self._refresh_free_space(rec)
        if rec.total_bytes and rec.free_bytes is not None and rec.free_bytes < (1 << 20):
            rec.status = "full"
        else:
            rec.status = "mounted"
        db.commit()
        return rec

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
        # with the disk deregistered those records are meaningless, so remove them.
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
        await self._refresh_free_space(rec)
        if not rec.free_bytes:
            raise BackupError("Backup disk is not mounted; cannot check capacity")
        return rec.free_bytes >= needed_bytes

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

        try:
            if not Path(rec.mount_point).is_mount():
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
                src = snap
            else:
                file_name = f"incr-{self._snap_ts(snap)}.{suffix}"
                src = f"-i {base_snapshot} {snap}"
            stream_file = str(dest_dir / file_name)

            # Capacity guard: estimate needed space vs free space.
            if backup_type == "full":
                needed = await self.estimate_needed(dataset_name)
                run.changed_bytes = 0  # full backups report changed=0; UI uses entire stream
            else:
                needed = await self.estimate_incremental_size(db, dataset_name)
            if not await self.check_capacity(db, backup_disk_id, needed):
                await run_zfs("destroy", snap, timeout=60, check=False)
                run.status = "failed"
                run.error = "Insufficient free space on backup disk"
                db.commit()
                return run

            gzip_level = self.settings.backup_gzip_level
            cmd = f"zfs send -R {src} | gzip -{gzip_level} > {shquote(stream_file)}"
            _, stderr, rc = await run_pipeline(cmd, timeout=86400, check=False)
            if rc != 0:
                await run_zfs("destroy", snap, timeout=60, check=False)
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
            run.status = "failed"
            run.error = str(e)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
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
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", "-t", "snapshot", "-r", dataset_name, check=False, op="read",
        )
        if rc != 0:
            return []
        names = []
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "@" in line and f"@{BACKUP_SNAP_PREFIX}" in line:
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

        # Names map uniquely back to their schedule row (dataset + type).
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
                desired[f"zfs-full-{s.dataset_name}"] = {**base_cfg, "cron": s.full_cron,
                                                         "type": "full", "retention": s.full_retention}
            if s.incremental_cron:
                desired[f"zfs-incr-{s.dataset_name}"] = {**base_cfg, "cron": s.incremental_cron,
                                                         "type": "incremental", "retention": s.incremental_retention}

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

    async def restore_dataset(self, stream_file: str, dataset_name: str) -> Dict[str, Any]:
        """Restore a dataset from a stream file (full or applying incrementals).

        Replays the full stream, then any matching incremental streams in order.
        """
        fp = Path(stream_file)
        if not fp.exists():
            raise BackupError(f"Stream file not found: {stream_file}")
        cmd = f"gunzip -c {shquote(str(fp))} | zfs receive -F {shquote(dataset_name)}"
        _, stderr, rc = await run_pipeline(cmd, timeout=86400, check=False)
        if rc != 0:
            raise BackupError(f"Restore failed: {stderr}")
        return {"dataset": dataset_name, "source": str(fp)}


def shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)


zfs_backup_manager = ZfsBackupManager()
