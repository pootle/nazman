from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers.smb_manager import smb_manager

router = APIRouter(prefix="/api/smb", tags=["smb"])


class SmbShareCreate(BaseModel):
    dataset_name: str
    read_only: bool = False
    enabled: bool = True


class SmbShareUpdate(BaseModel):
    read_only: Optional[bool] = None
    enabled: Optional[bool] = None


class SmbShareResponse(BaseModel):
    dataset_name: str
    share_name: str
    share_path: str
    read_only: bool = False
    guest_ok: bool = True
    enabled: bool = True


class PresenceResponse(BaseModel):
    installed: bool


class InstallResponse(BaseModel):
    installed: bool
    message: str


@router.get("/presence", response_model=PresenceResponse)
async def get_presence(
    current_user: dict = Depends(get_current_user),
):
    """Return whether Samba (smbd) is installed on this server."""
    return {"installed": smb_manager.is_server_present()}


@router.post("/install", response_model=InstallResponse)
async def install_server(
    current_user: dict = Depends(get_current_user),
):
    """Install Samba (smbd) on this server via apt."""
    try:
        return await smb_manager.install_server()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=List[SmbShareResponse])
async def list_shares(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List every dataset with a NAZMan-managed SMB share."""
    return smb_manager.list_shares(db)


@router.post("/", response_model=SmbShareResponse)
async def create_share(
    share: SmbShareCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Create/update a dataset's SMB share."""
    try:
        return await smb_manager.set_share(
            db,
            dataset_name=share.dataset_name,
            read_only=share.read_only,
            enabled=share.enabled,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{dataset_name:path}", response_model=SmbShareResponse)
async def update_share(
    dataset_name: str,
    update: SmbShareUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Update a dataset's SMB share (read-only toggle, enable/disable)."""
    existing = None
    for s in smb_manager.list_shares(db):
        if s["dataset_name"] == dataset_name:
            existing = s
            break
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No SMB share for '{dataset_name}'")

    try:
        return await smb_manager.set_share(
            db,
            dataset_name=dataset_name,
            read_only=update.read_only if update.read_only is not None else existing["read_only"],
            enabled=update.enabled if update.enabled is not None else existing["enabled"],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{dataset_name:path}")
async def delete_share(
    dataset_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Remove a dataset's SMB share."""
    try:
        await smb_manager.delete_share(db, dataset_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Share removed"}