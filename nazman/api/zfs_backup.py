from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_backup_manager
from ..models.backup_zfs import BackupDisk, BackupRun, BackupSchedule
from ..models.disk import Disk

router = APIRouter(prefix="/api/backup-zfs", tags=["backup-zfs"])


class BackupDiskResponse(BaseModel):
    id: int
    disk_id: int
    device_path: str
    fs_type: str
    mount_point: str
    fs_uuid: str
    total_bytes: int
    free_bytes: int
    status: str

    model_config = {"from_attributes": True}


class DeclareRequest(BaseModel):
    confirm: bool = False


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


class RestoreRunRequest(BaseModel):
    run_id: int = None
    stream_file: str = None
    dataset_name: str


@router.get("/disks", response_model=List[BackupDiskResponse])
async def list_backup_disks(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await zfs_backup_manager.list_backup_disks(db)


@router.get("/disks/candidates", response_model=List[dict])
async def backup_disk_candidates(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Disks eligible to be declared as a backup target (present, non-OS, not already declared)."""
    from ..managers.disk_manager import disk_manager as dm, get_device_name
    disks = await dm.sync_disks_to_database(db)
    declared = {d.disk_id for d in db.query(BackupDisk).all()}
    out = []
    for disk in disks:
        if disk.is_os_disk or disk.id in declared:
            continue
        out.append({
            "id": disk.id,
            "model": disk.model,
            "serial": disk.serial,
            "size_bytes": disk.size_bytes,
            "disk_type": disk.disk_type,
            "device_name": get_device_name(disk),
        })
    return out


@router.post("/disks/{disk_id}/declare", response_model=BackupDiskResponse)
async def declare_backup_disk(
    disk_id: int,
    req: DeclareRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        return await zfs_backup_manager.declare_backup_disk(db, disk_id, confirm=req.confirm)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disks/{backup_disk_id}/mount", response_model=BackupDiskResponse)
async def mount_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await zfs_backup_manager.mount_backup_disk(db, backup_disk_id)


@router.post("/disks/{backup_disk_id}/unmount", response_model=BackupDiskResponse)
async def unmount_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await zfs_backup_manager.unmount_backup_disk(db, backup_disk_id)


@router.post("/disks/{backup_disk_id}/scan", response_model=BackupDiskResponse)
async def scan_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await zfs_backup_manager.scan_backup_disk(db, backup_disk_id)


@router.delete("/disks/{backup_disk_id}")
async def deregister_backup_disk(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    await zfs_backup_manager.deregister_backup_disk(db, backup_disk_id)
    # Orphaned schedule-driven ScheduledTask jobs are removed by reconciliation.
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"message": "Backup disk deregistered"}


@router.get("/datasets", response_model=List[dict])
async def list_backupable_datasets(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """All datasets with their backup status and latest run info for the UI."""
    # Enumerate datasets live from ZFS; there is no DB table of datasets.
    from ..managers.nfs_manager import nfs_manager
    dataset_names = await nfs_manager._list_dataset_names()
    schedules = {s.dataset_name: s for s in db.query(BackupSchedule).all()}
    out = []
    for name in dataset_names:
        sched = schedules.get(name)
        last = (
            db.query(BackupRun)
            .filter(BackupRun.dataset_name == name)
            .order_by(BackupRun.id.desc())
            .first()
        )
        # Total data changed since the last full backup (sum of incremental
        # streams after the most recent full) — informs when to run the next full.
        changed_since_full = 0
        runs_since_full = (
            db.query(BackupRun)
            .filter(
                BackupRun.dataset_name == name,
                BackupRun.status == "success",
            )
            .order_by(BackupRun.id.desc())
            .all()
        )
        for r in runs_since_full:
            if r.backup_type == "full":
                break
            changed_since_full += r.changed_bytes or 0
        full_runs = (
            db.query(BackupRun)
            .filter(BackupRun.dataset_name == name, BackupRun.status == "success")
            .count()
        )
        out.append({
            "name": name,
            "full_cron": sched.full_cron if sched else None,
            "incremental_cron": sched.incremental_cron if sched else None,
            "enabled": sched.enabled if sched else False,
            "last_type": last.backup_type if last else None,
            "last_status": last.status if last else None,
            "last_changed_bytes": last.changed_bytes if last else 0,
            "changed_since_full": changed_since_full,
            "full_runs": full_runs,
        })
    return out


@router.get("/runs", response_model=List[BackupRunResponse])
async def list_backup_runs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return db.query(BackupRun).order_by(BackupRun.id.desc()).limit(200).all()


@router.post("/runs", response_model=BackupRunResponse)
async def run_backup(
    req: RunRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
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
    current_user: dict = Depends(get_current_user),
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
    current_user: dict = Depends(get_current_user),
):
    """Create or update a backup schedule for a dataset."""
    dataset_name = body.get("dataset_name")
    if not dataset_name:
        raise HTTPException(status_code=400, detail="dataset_name required")
    sched = db.query(BackupSchedule).filter(BackupSchedule.dataset_name == dataset_name).first()
    if not sched:
        sched = BackupSchedule(dataset_name=dataset_name)
        db.add(sched)
    sched.backup_disk_id = body.get("backup_disk_id")
    sched.full_cron = body.get("full_cron")
    sched.incremental_cron = body.get("incremental_cron")
    sched.full_retention = body.get("full_retention", 3)
    sched.incremental_retention = body.get("incremental_retention", 7)
    sched.enabled = body.get("enabled", True)
    db.commit()
    db.refresh(sched)

    # Reconcile ScheduledTask jobs so saved crons actually fire.
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"id": sched.id, "dataset_name": sched.dataset_name}


@router.delete("/schedules/{dataset_name:path}")
async def delete_backup_schedule(
    dataset_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Disable/remove a dataset's backup schedule (also unschedules its tasks)."""
    sched = db.query(BackupSchedule).filter(BackupSchedule.dataset_name == dataset_name).first()
    if sched:
        sched.enabled = False
        sched.full_cron = None
        sched.incremental_cron = None
        db.commit()
    await zfs_backup_manager.sync_scheduled_tasks(db)
    return {"message": "Schedule removed"}


@router.get("/disks/{backup_disk_id}/streams", response_model=List[dict])
async def list_disk_streams(
    backup_disk_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List backup stream files on a mounted backup disk (for new-server restore)."""
    return await zfs_backup_manager.list_stream_files(db, backup_disk_id)


@router.post("/restore-file")
async def restore_from_file(
    body: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Restore a dataset from an arbitrary stream file on a mounted backup disk,
    independent of any stored run record (same-server crash or brand-new server)."""
    stream_file = body.get("stream_file")
    dataset_name = body.get("dataset_name")
    if not stream_file or not dataset_name:
        raise HTTPException(status_code=400, detail="stream_file and dataset_name required")
    try:
        return await zfs_backup_manager.restore_dataset(stream_file, dataset_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/runs/{run_id}/restore")
async def restore_run(
    run_id: int,
    req: RestoreRunRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Restore a dataset from a stored stream file (full, or incremental chain)."""
    run = db.query(BackupRun).filter(BackupRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    dataset_name = req.dataset_name or run.dataset_name
    try:
        result = await zfs_backup_manager.restore_dataset(run.stream_file, dataset_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
