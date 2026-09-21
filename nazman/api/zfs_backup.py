from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
import logging
from datetime import datetime
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers.zfs_backup_manager import ZfsBackupManager
from ..wiring import get_zfs_backup_manager

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
    error: Optional[str] = None  # set while a declaration is reported failed

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
    stream_file: Optional[str] = None
    snapshot: Optional[str] = None
    base_snapshot: Optional[str]
    full_anchor: Optional[str]
    size_bytes: int
    changed_bytes: int
    phase: Optional[str] = None
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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    return await zfs_backup_manager.list_backup_disks(db)


@router.get("/disks/used")
async def list_used_disks(
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """(disk_id, slot_uuid) pairs unavailable as backup targets.

    Includes already-declared backup disks plus every device currently held
    by an imported pool (whole disks and partition members).  slot_uuid is
    None when the whole disk is held.  Used by the UI to disable those
    options in the backup-disk picker.
    """
    return await zfs_backup_manager.used_backup_targets(db)


@router.post("/disks/{disk_id}/declare", response_model=BackupDiskResponse, status_code=202)
async def declare_backup_disk(
    disk_id: int,
    req: DeclareRequest,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """Kick off a backup-disk declaration.

    The wipe+format runs in the background; this returns immediately with a
    ``pending`` entry that flips to the real disk (success) or ``failed``.
    """
    try:
        return await zfs_backup_manager.start_declare_backup_disk(
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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    return await zfs_backup_manager.scan_backup_disk(db, backup_disk_id)


@router.post("/disks/{backup_disk_id}/wake", response_model=BackupDiskResponse)
async def wake_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    return await zfs_backup_manager.update_backup_disk(
        db, backup_disk_id, unmount_after_backup=req.unmount_after_backup
    )


@router.delete("/disks/{backup_disk_id}")
async def deregister_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    await zfs_backup_manager.deregister_backup_disk(db, backup_disk_id)
    # Orphaned schedule-driven ScheduledTask jobs are removed by reconciliation.
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"message": "Backup disk deregistered"}


@router.get("/datasets", response_model=List[dict])
async def list_backupable_datasets(
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """All datasets with per-disk schedules, backup status, and run info."""
    return await zfs_backup_manager.list_backupable_datasets(db)


@router.get("/runs", response_model=List[BackupRunResponse])
async def list_backup_runs(
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    return await zfs_backup_manager.list_runs(db)


@router.post("/runs", response_model=BackupRunResponse, status_code=202)
async def run_backup(
    req: RunRequest,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    return await zfs_backup_manager.start_run_backup(
        db,
        dataset_name=req.dataset_name,
        backup_disk_id=req.backup_disk_id,
        backup_type=req.backup_type,
    )


@router.get("/schedules", response_model=List[dict])
async def list_backup_schedules(
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    return await zfs_backup_manager.list_schedules(db)


@router.post("/schedules")
async def upsert_backup_schedule(
    body: dict,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """Create or update the backup schedule for a (dataset, disk) pair."""
    sched = await zfs_backup_manager.upsert_schedule(db, body)
    return {"id": sched.id, "dataset_name": sched.dataset_name, "backup_disk_id": sched.backup_disk_id}


@router.delete("/schedules/{dataset_name:path}")
async def delete_backup_schedule(
    dataset_name: str,
    backup_disk_id: Optional[int] = None,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """Remove the dataset's schedule on one disk (or all disks when no disk given)."""
    removed = await zfs_backup_manager.delete_schedules(db, dataset_name, backup_disk_id)
    return {"message": "Schedule removed", "backup_disk_ids": removed}


@router.get("/disks/{backup_disk_id}/streams", response_model=List[dict])
async def list_disk_streams(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """List backup stream files on a mounted backup disk (for new-server restore)."""
    return await zfs_backup_manager.list_stream_files(db, backup_disk_id)


@router.get("/disks/{backup_disk_id}/manifest", response_model=dict)
async def get_disk_manifest(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """The volume's aggregate backup manifest (pools, datasets, config)."""
    try:
        return await zfs_backup_manager.get_volume_manifest(db, backup_disk_id)
    except Exception as e:
        logger.error("get_manifest failed for disk %s: %s", backup_disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disks/{backup_disk_id}/rebuild-manifest", response_model=dict)
async def rebuild_disk_manifest(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """Regenerate the manifest by scanning streams and sidecars on the volume."""
    try:
        return await zfs_backup_manager.rebuild_manifest(db, backup_disk_id)
    except Exception as e:
        logger.error("rebuild_manifest failed for disk %s: %s", backup_disk_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/restore-file")
async def restore_from_file(
    req: RestoreFileRequest,
    db: Session = Depends(get_db),
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

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
    zfs_backup_manager: ZfsBackupManager = Depends(get_zfs_backup_manager),

):
    """Restore a dataset from a stored stream file (full, or incremental chain)."""
    run = zfs_backup_manager.get_run(db, run_id)
    try:
        result = await zfs_backup_manager.restore_dataset(
            db, run.stream_file, req.target_dataset, force=req.force
        )
    except Exception as e:
        logger.error("restore_run %s failed: %s", run_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    return result
