from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers.backup_manager import BackupManager
from ..wiring import get_backup_manager

router = APIRouter(prefix="/api/backup", tags=["backup"], dependencies=[Depends(get_current_user)])


class RestoreRequest(BaseModel):
    commit_hash: str


@router.get("/bundles", response_model=List[dict])
async def list_config_bundles(
    db: Session = Depends(get_db),
    backup_manager: BackupManager = Depends(get_backup_manager),
):
    """Config bundles available across all declared backup volumes, newest first."""
    return backup_manager.find_config_bundles(db)


@router.post("/restore")
async def restore_backup(
    request: RestoreRequest,
    db: Session = Depends(get_db),
    backup_manager: BackupManager = Depends(get_backup_manager),
):
    """Restore configuration from the bundle with the given id."""
    success = await backup_manager.restore_configuration(db, request.commit_hash)
    if success:
        return {"message": f"Configuration restored from bundle {request.commit_hash}"}
    return {"message": "Restore failed"}
