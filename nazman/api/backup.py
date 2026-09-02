from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import backup_manager
from ..models.backup import BackupCommit

router = APIRouter(prefix="/api/backup", tags=["backup"])


class BackupCommitResponse(BaseModel):
    id: int
    commit_hash: str
    commit_message: str
    author: str
    files_changed: int
    
    model_config = {"from_attributes": True}


class BackupStatusResponse(BaseModel):
    repo_exists: bool
    repo_path: str
    last_commit: Optional[dict]
    has_uncommitted_changes: bool
    backup_enabled: bool


class RestoreRequest(BaseModel):
    commit_hash: str


@router.get("/status", response_model=BackupStatusResponse)
async def get_backup_status(
    current_user: dict = Depends(get_current_user)
):
    """Get backup system status."""
    return await backup_manager.get_backup_status()


@router.get("/history", response_model=List[BackupCommitResponse])
async def get_backup_history(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Get backup commit history."""
    return await backup_manager.get_backup_history(db, limit)


@router.post("/backup", response_model=BackupCommitResponse)
async def create_backup(
    message: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Create a new backup."""
    return await backup_manager.backup_configuration(db, message)


@router.post("/restore")
async def restore_backup(
    request: RestoreRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Restore configuration from a specific commit."""
    success = await backup_manager.restore_configuration(db, request.commit_hash)
    if success:
        return {"message": f"Configuration restored from commit {request.commit_hash}"}
    else:
        return {"message": "Restore failed"}
