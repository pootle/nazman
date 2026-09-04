from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import nfs_manager

router = APIRouter(prefix="/api/nfs", tags=["nfs"])


class NfsShareCreate(BaseModel):
    dataset_name: str
    client_spec: Optional[str] = None
    options: Optional[dict] = None


class NfsShareUpdate(BaseModel):
    client_spec: Optional[str] = None
    options: Optional[dict] = None
    enabled: Optional[bool] = None
    sharenfs: Optional[str] = None


class NfsShareResponse(BaseModel):
    dataset_name: str
    export_path: str
    sharenfs: str
    enabled: bool


class ActiveExportResponse(BaseModel):
    path: str
    client: str
    options: str


@router.get("/", response_model=List[NfsShareResponse])
async def list_exports(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List every dataset and its live ZFS sharenfs value."""
    return await nfs_manager.list_exports(db)


@router.get("/active", response_model=List[ActiveExportResponse])
async def list_active_exports(
    current_user: dict = Depends(get_current_user),
):
    """List currently active NFS exports from the kernel export table."""
    return await nfs_manager.get_active_exports()


@router.post("/", response_model=NfsShareResponse)
async def create_export(
    share: NfsShareCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Create/update a dataset's NFS share via sharenfs."""
    try:
        return await nfs_manager.set_export(
            db,
            dataset_name=share.dataset_name,
            client_spec=share.client_spec,
            options=share.options,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{dataset_name:path}", response_model=NfsShareResponse)
async def update_export(
    dataset_name: str,
    update: NfsShareUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Update a dataset's NFS share (options, client, enable/disable)."""
    try:
        return await nfs_manager.set_export(
            db,
            dataset_name=dataset_name,
            client_spec=update.client_spec,
            options=update.options,
            sharenfs=update.sharenfs,
            enabled=update.enabled,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{dataset_name:path}")
async def delete_export(
    dataset_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Disable (unshare) a dataset's NFS share."""
    try:
        await nfs_manager.delete_export(db, dataset_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Export disabled"}


@router.get("/presence")
async def get_presence(
    current_user: dict = Depends(get_current_user),
):
    """Return whether the NFS kernel server (exportfs) is installed."""
    return {"installed": nfs_manager.is_server_present()}


class InstallResponse(BaseModel):
    installed: bool
    message: str


@router.post("/install", response_model=InstallResponse)
async def install_server(
    current_user: dict = Depends(get_current_user),
):
    """Install the NFS kernel server (nfs-kernel-server) on this server via apt."""
    try:
        return await nfs_manager.install_server()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
