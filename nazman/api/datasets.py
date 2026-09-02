from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_manager
from ..utils.commands import run_zfs
from ..utils.exceptions import DatasetError
from ..managers.zfs_manager import _atime_to_params

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


class DatasetCreate(BaseModel):
    name: str
    pool_name: str
    compression: str = "zstd"
    atime: str = "partial"
    sync_mode: str = "standard"
    quota: Optional[str] = None
    recordsize: str = "128K"
    canmount: str = "on"
    readonly: str = "off"
    special_small_blocks: Optional[str] = None


class DatasetUpdate(BaseModel):
    compression: Optional[str] = None
    recordsize: Optional[str] = None
    sync_mode: Optional[str] = None
    quota: Optional[str] = None
    special_small_blocks: Optional[str] = None
    atime: Optional[str] = None
    canmount: Optional[str] = None
    readonly: Optional[str] = None


class DatasetResponse(BaseModel):
    name: str
    compression: Optional[str] = None
    recordsize: Optional[str] = None
    sync_mode: Optional[str] = None
    quota: Optional[str] = None
    special_small_blocks: Optional[str] = None
    atime: Optional[str] = None
    canmount: Optional[str] = None
    readonly: Optional[str] = None
    mountpoint: Optional[str] = None
    used: Optional[str] = None
    available: Optional[str] = None
    referenced: Optional[str] = None
    created_at: Optional[str] = None


@router.get("/", response_model=List[DatasetResponse])
async def list_datasets(
    pool_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """List all datasets."""
    return await zfs_manager.list_datasets(db, pool_name)


@router.get("/{dataset_name:path}", response_model=DatasetResponse)
async def get_dataset(
    dataset_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Get dataset by name with live ZFS properties."""
    if not await zfs_manager._dataset_live_exists(dataset_name):
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Get live ZFS properties
    live_props = await zfs_manager._get_dataset_properties(dataset_name)

    return {
        "name": dataset_name,
        **live_props,
    }


@router.post("/", response_model=DatasetResponse)
async def create_dataset(
    dataset: DatasetCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Create a new dataset."""
    return await zfs_manager.create_dataset(
        db,
        name=dataset.name,
        pool_name=dataset.pool_name,
        compression=dataset.compression,
        recordsize=dataset.recordsize,
        sync_mode=dataset.sync_mode,
        quota=dataset.quota,
        special_small_blocks=dataset.special_small_blocks,
        atime=dataset.atime,
        canmount=dataset.canmount,
        readonly=dataset.readonly
    )


@router.put("/{dataset_name:path}", response_model=DatasetResponse)
async def update_dataset(
    dataset_name: str,
    update: DatasetUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Update dataset properties via zfs set (no DB persistence for ZFS properties)."""
    if not await zfs_manager._dataset_live_exists(dataset_name):
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Apply property changes via zfs set
    if update.compression is not None:
        await run_zfs("set", f"compression={update.compression}", dataset_name)

    if update.recordsize is not None:
        await run_zfs("set", f"recordsize={update.recordsize}", dataset_name)

    if update.sync_mode is not None:
        await run_zfs("set", f"sync={update.sync_mode}", dataset_name)

    if update.quota is not None:
        if update.quota:
            await run_zfs("set", f"quota={update.quota}", dataset_name)
        else:
            await run_zfs("set", "quota=none", dataset_name)

    if update.special_small_blocks is not None:
        val = update.special_small_blocks.strip()
        await run_zfs(
            "set", f"special_small_blocks={val or '0'}", dataset_name
        )

    if update.atime is not None:
        for tok in _atime_to_params(update.atime):
            await run_zfs("set", tok, dataset_name)

    if update.canmount is not None:
        await run_zfs("set", f"canmount={update.canmount}", dataset_name)

    if update.readonly is not None:
        await run_zfs("set", f"readonly={update.readonly}", dataset_name)

    # Return dataset with live ZFS properties
    live_props = await zfs_manager._get_dataset_properties(dataset_name)
    return {
        "name": dataset_name,
        **live_props,
    }


@router.delete("/{dataset_name:path}")
async def destroy_dataset(
    dataset_name: str,
    recursive: bool = False,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Destroy a dataset (DESTRUCTIVE)."""
    try:
        await zfs_manager.destroy_dataset(db, dataset_name, recursive)
    except DatasetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": f"Dataset {dataset_name} destroyed"}
