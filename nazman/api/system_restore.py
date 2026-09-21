from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import logging

from ..database import get_db
from ..auth import get_current_user
from ..services.system_restore import SystemRestoreService
from ..wiring import get_system_restore_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/system-restore", tags=["system-restore"],
    dependencies=[Depends(get_current_user)],
)


class CreatePoolRequest(BaseModel):
    vdevs: List[Dict[str, Any]]


class RestoreDatasetsRequest(BaseModel):
    selections: List[Dict[str, Any]]
    media_fs_uuid: Optional[str] = None


class RestoreConfigRequest(BaseModel):
    config_id: str


@router.get("/sets", response_model=List[dict])
async def list_backup_sets(
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Scan all attached disks and list available backup info sets."""
    return await service.discover_backup_sets(db)


@router.get("/sets/{set_id}", response_model=dict)
async def get_backup_set(
    set_id: str,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Full detail of one backup set: pools/vdevs, datasets, config, media."""
    return await service.get_backup_set(db, set_id)


@router.get("/sets/{set_id}/pools/{pool_name}/plan", response_model=dict)
async def plan_pool(
    set_id: str,
    pool_name: str,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Suggested attached-disk mapping for each recorded vdev slot."""
    return await service.plan_pool_mapping(db, set_id, pool_name)


@router.post("/sets/{set_id}/pools/{pool_name}/create", response_model=dict)
async def create_pool(
    set_id: str,
    pool_name: str,
    req: CreatePoolRequest,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Recreate a pool from an operator-confirmed vdev/device mapping."""
    return await service.create_pool_from_backup(db, set_id, pool_name, req.vdevs)


@router.get("/sets/{set_id}/datasets/plan", response_model=List[dict])
async def dataset_restore_plan(
    set_id: str,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Datasets to restore with suggested target pools (all default enabled)."""
    return await service.restore_plan(db, set_id)


@router.get("/sets/{set_id}/media", response_model=List[dict])
async def required_media(
    set_id: str,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Backup media required by the set, grouped by filesystem UUID."""
    return await service.required_media(db, set_id)


@router.post("/sets/{set_id}/datasets/restore", response_model=dict)
async def restore_datasets(
    set_id: str,
    req: RestoreDatasetsRequest,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Replay selected datasets' chains; optionally restrict to one medium."""
    try:
        return await service.restore_datasets(
            db, set_id, req.selections, media_fs_uuid=req.media_fs_uuid,
        )
    except Exception as e:
        logger.error("restore_datasets failed for set %s: %s", set_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sets/{set_id}/config/restore", response_model=dict)
async def restore_configuration(
    set_id: str,
    req: RestoreConfigRequest,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Restore the nazman database and host config from a config bundle."""
    try:
        return await service.restore_configuration(db, set_id, req.config_id)
    except Exception as e:
        logger.error("restore_configuration failed for set %s: %s", set_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sets/{set_id}/adopt", response_model=dict)
async def adopt_media(
    set_id: str,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Re-register the backup volume as a declared backup disk."""
    try:
        return await service.adopt_media(db, set_id)
    except Exception as e:
        logger.error("adopt_media failed for set %s: %s", set_id, e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sets/{set_id}/rebuild-schedules", response_model=dict)
async def rebuild_schedules(
    set_id: str,
    db: Session = Depends(get_db),
    service: SystemRestoreService = Depends(get_system_restore_service),
):
    """Reconcile restored backup schedules into scheduler jobs."""
    return await service.rebuild_schedules(db, set_id)
