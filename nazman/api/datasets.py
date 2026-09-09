from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_manager
from ..utils.exceptions import DatasetError

router = APIRouter(prefix="/api/datasets", tags=["datasets"], dependencies=[Depends(get_current_user)])


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

):
    """List all datasets."""
    return await zfs_manager.list_datasets(db, pool_name)


@router.get("/{dataset_name:path}", response_model=DatasetResponse)
async def get_dataset(
    dataset_name: str,
    db: Session = Depends(get_db),

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

):
    """Update dataset properties via zfs set (no DB persistence for ZFS properties)."""
    try:
        return await zfs_manager.update_dataset(
            dataset_name,
            compression=update.compression,
            recordsize=update.recordsize,
            sync_mode=update.sync_mode,
            quota=update.quota,
            special_small_blocks=update.special_small_blocks,
            atime=update.atime,
            canmount=update.canmount,
            readonly=update.readonly,
        )
    except DatasetError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{dataset_name:path}")
async def destroy_dataset(
    dataset_name: str,
    recursive: bool = False,
    db: Session = Depends(get_db),

):
    """Destroy a dataset (DESTRUCTIVE)."""
    try:
        await zfs_manager.destroy_dataset(db, dataset_name, recursive)
    except DatasetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": f"Dataset {dataset_name} destroyed"}
