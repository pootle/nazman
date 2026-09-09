from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
import logging
import re
from datetime import datetime
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_backup_manager
from ..managers.disk_manager import get_device_path
from ..models.backup_zfs import BackupDisk, BackupRun, BackupSchedule
from ..models.disk import Disk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup-zfs", tags=["backup-zfs"], dependencies=[Depends(get_current_user)])


class BackupDiskResponse(BaseModel):
    id: int
    disk_id: int
    slot_uuid: Optional[str] = None
    partition_number: int = 1
    device_path: Optional[str] = None  # derived from disks.by_id + partition_number
    label: Optional[str] = None
    fs_type: str
    mount_point: str
    fs_uuid: str
    total_bytes: int = 0  # live probe, never stored
    free_bytes: int = 0  # live probe, never stored
    status: str  # live probe, never stored
    unmount_after_backup: bool

    model_config = {"from_attributes": True}


class UpdateBackupDiskRequest(BaseModel):
    unmount_after_backup: Optional[bool] = None


class DeclareRequest(BaseModel):
    confirm: bool = False
    slot_uuid: Optional[str] = None
    label: Optional[str] = None
    wipe_raid: bool = False


class BackupRunResponse(BaseModel):
    id: int
    dataset_name: str
    backup_disk_id: Optional[int]
    backup_type: str
    stream_file: str
    snapshot: str
    base_snapshot: Optional[str]
    full_anchor: Optional[str]
    size_bytes: int
    changed_bytes: int
    status: str
    error: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class RunRequest(BaseModel):
    dataset_name: str
    backup_disk_id: int
    backup_type: str = "full"


class RestoreFileRequest(BaseModel):
    stream_file: str
    target_dataset: str
    force: bool = False


class RestoreRunRequest(BaseModel):
    target_dataset: str
    force: bool = False


@router.get("/disks", response_model=List[BackupDiskResponse])
async def list_backup_disks(
    db: Session = Depends(get_db),

):
    return await zfs_backup_manager.list_backup_disks(db)


@router.get("/disks/used")
async def list_used_disks(
    db: Session = Depends(get_db),

):
    """(disk_id, slot_uuid) pairs unavailable as backup targets.

    Includes already-declared backup disks plus every device currently held
    by an imported pool (whole disks and partition members).  slot_uuid is
    None when the whole disk is held.  Used by the UI to disable those
    options in the backup-disk picker.
    """
    used = []
    seen = set()
    for d in db.query(BackupDisk).all():
        key = (d.disk_id, d.slot_uuid)
        if key not in seen:
            seen.add(key)
            used.append({"disk_id": d.disk_id, "slot_uuid": d.slot_uuid})
    for entry in await _pool_member_usage(db):
        key = (entry["disk_id"], entry["slot_uuid"])
        if key not in seen:
            seen.add(key)
            used.append(entry)
    return used


async def _pool_member_usage(db: Session) -> List[dict]:
    """Map every device held by an imported pool to (disk_id, slot_uuid)."""
    from ..managers.zfs_manager import zfs_manager
    members = await zfs_manager.get_pool_members()
    results = []
    seen = set()
    for dev in members:
        entry = await _usage_entry_for_device(db, dev)
        if not entry:
            continue
        key = (entry["disk_id"], entry["slot_uuid"])
        if key not in seen:
            seen.add(key)
            results.append(entry)
    return results


async def _usage_entry_for_device(db: Session, dev: str) -> Optional[dict]:
    """Resolve a zpool-reported device to the disk it occupies.

    A device held by a pool (whole disk, or a partition on a disk) claims the
    whole disk: ZFS owns the disk, so none of its partitions may be offered as
    a backup target.  Always returns slot_uuid=None.
    """
    disk = None
    if dev.startswith("/dev/disk/by-id/"):
        m = re.search(r"-part(\d+)$", dev)
        base = dev
        if m:
            base = dev[:m.start()]
        disk = db.query(Disk).filter(Disk.by_id == base).first()
    else:
        # by-id basename (zpool drops the /dev/disk/by-id/ prefix, and for
        # partitioned vdevs also the -partN suffix); otherwise a kernel name.
        disk = db.query(Disk).filter(Disk.by_id == f"/dev/disk/by-id/{dev}").first()
        if not disk:
            disk = _find_disk_by_device_path(db, f"/dev/{dev}")
        if not disk and dev:
            base = _kernel_base_name(dev)
            if base != dev:
                disk = _find_disk_by_device_path(db, f"/dev/{base}")
    if not disk:
        return None
    return {"disk_id": disk.id, "slot_uuid": None}


def _find_disk_by_device_path(db: Session, dev_path: str) -> Optional[Disk]:
    for d in db.query(Disk).all():
        if get_device_path(d) == dev_path:
            return d
    return None


def _kernel_base_name(name: str) -> str:
    """Strip trailing partition digits (and 'p' prefix) from a kernel name."""
    m = re.search(r"(?:p\d+|\d+)$", name)
    return name[:m.start()] if m else name


@router.post("/disks/{disk_id}/declare", response_model=BackupDiskResponse)
async def declare_backup_disk(
    disk_id: int,
    req: DeclareRequest,
    db: Session = Depends(get_db),

):
    try:
        return await zfs_backup_manager.declare_backup_disk(
            db, disk_id, confirm=req.confirm,
            slot_uuid=req.slot_uuid, label=req.label,
            wipe_raid=req.wipe_raid,
        )
    except Exception as e:
        logger.error("declare_backup_disk failed for disk %s: %s", disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/disks/{disk_id}/raid-info")
async def disk_raid_info(
    disk_id: int,
    slot_uuid: Optional[str] = None,
    db: Session = Depends(get_db),

):
    """Software RAID metadata on the device before a destructive declare."""
    try:
        return await zfs_backup_manager.get_raid_info(db, disk_id, slot_uuid=slot_uuid)
    except Exception as e:
        logger.error("raid-info failed for disk %s: %s", disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disks/{backup_disk_id}/mount", response_model=BackupDiskResponse)
async def mount_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),

):
    try:
        return await zfs_backup_manager.mount_backup_disk(db, backup_disk_id)
    except Exception as e:
        logger.error("mount backup disk %s failed: %s", backup_disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disks/{backup_disk_id}/unmount", response_model=BackupDiskResponse)
async def unmount_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),

):
    try:
        return await zfs_backup_manager.unmount_backup_disk(db, backup_disk_id)
    except Exception as e:
        logger.error("unmount backup disk %s failed: %s", backup_disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disks/{backup_disk_id}/scan", response_model=BackupDiskResponse)
async def scan_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),

):
    return await zfs_backup_manager.scan_backup_disk(db, backup_disk_id)


@router.post("/disks/{backup_disk_id}/wake", response_model=BackupDiskResponse)
async def wake_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),

):
    """Software wake/replug: force a missing backup disk's bridge to re-enumerate."""
    try:
        return await zfs_backup_manager.wake_backup_disk(db, backup_disk_id)
    except Exception as e:
        logger.error("wake backup disk %s failed: %s", backup_disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/disks/{backup_disk_id}", response_model=BackupDiskResponse)
async def update_backup_disk(
    backup_disk_id: int,
    req: UpdateBackupDiskRequest,
    db: Session = Depends(get_db),

):
    rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Backup disk not found")
    if req.unmount_after_backup is not None:
        rec.unmount_after_backup = req.unmount_after_backup
    db.commit()
    db.refresh(rec)
    return await zfs_backup_manager._serialize_now(rec)


@router.delete("/disks/{backup_disk_id}")
async def deregister_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),

):
    await zfs_backup_manager.deregister_backup_disk(db, backup_disk_id)
    # Orphaned schedule-driven ScheduledTask jobs are removed by reconciliation.
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"message": "Backup disk deregistered"}


@router.get("/datasets", response_model=List[dict])
async def list_backupable_datasets(
    db: Session = Depends(get_db),

):
    """All datasets with per-disk schedules, backup status, and run info."""
    # Enumerate datasets live from ZFS; there is no DB table of datasets.
    from ..managers.nfs_manager import nfs_manager
    dataset_names = await nfs_manager._list_dataset_names()

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


@router.get("/runs", response_model=List[BackupRunResponse])
async def list_backup_runs(
    db: Session = Depends(get_db),

):
    return db.query(BackupRun).order_by(BackupRun.id.desc()).limit(200).all()


@router.post("/runs", response_model=BackupRunResponse)
async def run_backup(
    req: RunRequest,
    db: Session = Depends(get_db),

):
    return await zfs_backup_manager.run_backup(
        db,
        dataset_name=req.dataset_name,
        backup_disk_id=req.backup_disk_id,
        backup_type=req.backup_type,
    )


@router.get("/schedules", response_model=List[dict])
async def list_backup_schedules(
    db: Session = Depends(get_db),

):
    rows = []
    for s in db.query(BackupSchedule).all():
        rows.append({
            "id": s.id, "dataset_name": s.dataset_name,
            "backup_disk_id": s.backup_disk_id,
            "full_cron": s.full_cron, "incremental_cron": s.incremental_cron,
            "full_retention": s.full_retention,
            "incremental_retention": s.incremental_retention,
            "enabled": s.enabled,
        })
    return rows


@router.post("/schedules")
async def upsert_backup_schedule(
    body: dict,
    db: Session = Depends(get_db),

):
    """Create or update the backup schedule for a (dataset, disk) pair."""
    dataset_name = body.get("dataset_name")
    backup_disk_id = body.get("backup_disk_id")
    if not dataset_name:
        raise HTTPException(status_code=400, detail="dataset_name required")
    if not backup_disk_id:
        raise HTTPException(status_code=400, detail="backup_disk_id required")
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
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"id": sched.id, "dataset_name": sched.dataset_name, "backup_disk_id": sched.backup_disk_id}


@router.delete("/schedules/{dataset_name:path}")
async def delete_backup_schedule(
    dataset_name: str,
    backup_disk_id: Optional[int] = None,
    db: Session = Depends(get_db),

):
    """Remove the dataset's schedule on one disk (or all disks when no disk given)."""
    query = db.query(BackupSchedule).filter(BackupSchedule.dataset_name == dataset_name)
    if backup_disk_id is not None:
        query = query.filter(BackupSchedule.backup_disk_id == backup_disk_id)
    removed = []
    for sched in query.all():
        removed.append(sched.backup_disk_id)
        db.delete(sched)
    db.commit()
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"message": "Schedule removed", "backup_disk_ids": removed}


@router.get("/disks/{backup_disk_id}/streams", response_model=List[dict])
async def list_disk_streams(
    backup_disk_id: int,
    db: Session = Depends(get_db),

):
    """List backup stream files on a mounted backup disk (for new-server restore)."""
    return await zfs_backup_manager.list_stream_files(db, backup_disk_id)


@router.post("/restore-file")
async def restore_from_file(
    req: RestoreFileRequest,
    db: Session = Depends(get_db),

):
    """Restore a dataset from an arbitrary stream file on a mounted backup disk,
    independent of any stored run record (same-server crash or brand-new server)."""
    try:
        return await zfs_backup_manager.restore_dataset(
            db, req.stream_file, req.target_dataset, force=req.force
        )
    except Exception as e:
        logger.error("restore_from_file failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/runs/{run_id}/restore")
async def restore_run(
    run_id: int,
    req: RestoreRunRequest,
    db: Session = Depends(get_db),

):
    """Restore a dataset from a stored stream file (full, or incremental chain)."""
    run = db.query(BackupRun).filter(BackupRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        result = await zfs_backup_manager.restore_dataset(
            db, run.stream_file, req.target_dataset, force=req.force
        )
    except Exception as e:
        logger.error("restore_run %s failed: %s", run_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    return result
