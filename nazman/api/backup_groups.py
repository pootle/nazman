"""Backup group endpoints: groups, datasets, sets, and the backup cycle.

Everything that starts a backup lives here, because a backup is now a property
of a *group* (a fixed dataset list) landing on the group's active *set* (a chain
of disks) - not of a single (dataset, disk) pair.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..services.backup_group_service import BackupGroupService
from ..wiring import get_backup_group_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/backup-groups",
    tags=["backup-groups"],
    dependencies=[Depends(get_current_user)],
)


class CreateGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    datasets: List[str] = Field(default_factory=list)
    full_cron: Optional[str] = None
    incremental_cron: Optional[str] = None
    enabled: bool = True
    copies: int = Field(default=1, ge=1)
    recycle_full_disks: bool = False


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    full_cron: Optional[str] = None
    incremental_cron: Optional[str] = None
    enabled: Optional[bool] = None
    copies: Optional[int] = Field(default=None, ge=1)
    recycle_full_disks: Optional[bool] = None


class CreateSetRequest(BaseModel):
    label: Optional[str] = None
    position: Optional[int] = None


class UpdateSetRequest(BaseModel):
    label: Optional[str] = None
    position: Optional[int] = None


class DatasetRequest(BaseModel):
    dataset_name: str


class StartBackupRequest(BaseModel):
    backup_type: str = "full"


def _fail(e: Exception) -> HTTPException:
    logger.error("backup group request failed: %s", e, exc_info=True)
    return HTTPException(status_code=400, detail=str(e))


# ── Groups ─────────────────────────────────────────────────────────────
@router.get("")
async def list_groups(
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Every group with datasets, sets, and live per-disk capacity."""
    return await svc.list_groups(db)


@router.post("", status_code=201)
async def create_group(
    body: CreateGroupRequest,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Create a group.  Sets and disks are added afterwards."""
    try:
        group = await svc.create_group(
            db, body.name, body.datasets,
            full_cron=body.full_cron, incremental_cron=body.incremental_cron,
            enabled=body.enabled, copies=body.copies,
            recycle_full_disks=body.recycle_full_disks,
        )
        return await svc.describe_group(db, group.id)
    except Exception as e:
        raise _fail(e)


@router.get("/{group_id}")
async def get_group(
    group_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    try:
        return await svc.describe_group(db, group_id)
    except Exception as e:
        raise _fail(e)


@router.patch("/{group_id}")
async def update_group(
    group_id: int,
    body: UpdateGroupRequest,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    try:
        await svc.update_group(
            db, group_id, name=body.name, full_cron=body.full_cron,
            incremental_cron=body.incremental_cron, enabled=body.enabled,
            copies=body.copies, recycle_full_disks=body.recycle_full_disks,
        )
        return await svc.describe_group(db, group_id)
    except Exception as e:
        raise _fail(e)


@router.delete("/{group_id}")
async def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Delete a group and its sets.  Its disks survive, unassigned."""
    try:
        return await svc.delete_group(db, group_id)
    except Exception as e:
        raise _fail(e)


# ── Datasets ───────────────────────────────────────────────────────────
@router.post("/{group_id}/datasets", status_code=201)
async def add_dataset(
    group_id: int,
    body: DatasetRequest,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    try:
        return await svc.add_dataset(db, group_id, body.dataset_name)
    except Exception as e:
        raise _fail(e)


@router.delete("/{group_id}/datasets/{dataset_name:path}")
async def remove_dataset(
    group_id: int,
    dataset_name: str,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    try:
        return await svc.remove_dataset(db, group_id, dataset_name)
    except Exception as e:
        raise _fail(e)


# ── Sets ───────────────────────────────────────────────────────────────
@router.post("/{group_id}/sets", status_code=201)
async def create_set(
    group_id: int,
    body: CreateSetRequest,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Add a rotation slot.  A group's sets are used in order and wrap around."""
    try:
        bset = await svc.create_set(db, group_id, label=body.label, position=body.position)
        return await svc.describe_set(db, bset.id)
    except Exception as e:
        raise _fail(e)


@router.get("/{group_id}/sets")
async def list_sets(
    group_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    return [await svc.describe_set(db, s.id) for s in svc.group_sets(db, group_id)]


@router.get("/{group_id}/sets/{set_id}")
async def get_set(
    group_id: int,
    set_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    try:
        return await svc.describe_set(db, set_id)
    except Exception as e:
        raise _fail(e)


@router.patch("/{group_id}/sets/{set_id}")
async def update_set(
    group_id: int,
    set_id: int,
    body: UpdateSetRequest,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    try:
        bset = await svc.update_set(db, set_id, label=body.label, position=body.position)
        return await svc.describe_set(db, bset.id)
    except Exception as e:
        raise _fail(e)


@router.delete("/{group_id}/sets/{set_id}")
async def delete_set(
    group_id: int,
    set_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Remove a rotation slot.  Its disks and their data survive, unassigned."""
    try:
        return await svc.delete_set(db, set_id)
    except Exception as e:
        raise _fail(e)


@router.post("/{group_id}/sets/{set_id}/activate")
async def activate_set(
    group_id: int,
    set_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Make this the set the next backup writes to."""
    try:
        result = await svc.activate_set(db, set_id)
        return await svc.describe_group(db, result["group_id"])
    except Exception as e:
        raise _fail(e)


@router.post("/{group_id}/sets/{set_id}/disks/{backup_disk_id}", status_code=201)
async def attach_disk(
    group_id: int,
    set_id: int,
    backup_disk_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """File an already-declared volume into a set."""
    try:
        return await svc.attach_disk(db, set_id, backup_disk_id)
    except Exception as e:
        raise _fail(e)


@router.delete("/{group_id}/sets/{set_id}/disks/{backup_disk_id}")
async def detach_disk(
    group_id: int,
    set_id: int,
    backup_disk_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Remove a volume from a set and undeclare it.

    The response lists any datasets whose chain referenced snapshots on it -
    those can no longer be restored from the remaining media.
    """
    try:
        return await svc.detach_disk(db, set_id, backup_disk_id)
    except Exception as e:
        raise _fail(e)


@router.post("/{group_id}/sets/{set_id}/advance")
async def advance_disk(
    group_id: int,
    set_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Move the set to its next disk by hand."""
    try:
        return await svc.advance_disk(db, set_id)
    except Exception as e:
        raise _fail(e)


@router.post("/{group_id}/sets/{set_id}/disks/{backup_disk_id}/activate")
async def set_active_disk(
    group_id: int,
    set_id: int,
    backup_disk_id: int,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Make a specific disk in the set the writer from the next run on."""
    try:
        return await svc.set_active_disk(db, set_id, backup_disk_id)
    except Exception as e:
        raise _fail(e)


# ── Running a backup ───────────────────────────────────────────────────
@router.post("/{group_id}/backup", status_code=202)
async def start_backup(
    group_id: int,
    body: StartBackupRequest,
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Start a session: every dataset in the group onto the active set.

    Returns as soon as the session is queued, so the UI can show it running.
    """
    try:
        session = await svc.start_session(db, group_id, body.backup_type)
        return {
            "session_id": session.id,
            "group_id": group_id,
            "trigger": session.trigger,
            "status": session.status,
        }
    except Exception as e:
        raise _fail(e)


@router.get("/{group_id}/sessions")
async def list_sessions(
    group_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Session history for a group, newest first, with per-dataset runs."""
    try:
        return await svc.list_sessions(db, group_id=group_id, limit=limit)
    except Exception as e:
        raise _fail(e)


@router.get("/sessions/all")
async def list_all_sessions(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    svc: BackupGroupService = Depends(get_backup_group_service),
):
    """Session history across every group."""
    try:
        return await svc.list_sessions(db, limit=limit)
    except Exception as e:
        raise _fail(e)
