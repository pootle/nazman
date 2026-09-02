from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_manager
from ..models.pool import Pool

router = APIRouter(prefix="/api/pools", tags=["pools"])


class DeviceSpec(BaseModel):
    disk_id: int
    slot_uuid: Optional[str] = None  # None = whole disk


class VdevSpec(BaseModel):
    role: str  # data, log, cache, special
    topology: str  # stripe, mirror, raidz1, raidz2, raidz3
    devices: List[DeviceSpec]
    ashift: Optional[int] = None  # per-vdev ashift; None = inherit global


class PoolCreate(BaseModel):
    name: str
    vdevs: List[VdevSpec]
    ashift: int = 12


class PoolResponse(BaseModel):
    id: int
    name: str
    status: Optional[str] = None
    health: Optional[str] = None
    topology: Optional[str] = None
    size_bytes: Optional[int] = None
    allocated_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    usable_bytes: Optional[int] = None
    used_capacity_pct: Optional[float] = None
    compressratio: Optional[float] = None
    datasets: List[dict] = []
    created_at: Optional[str] = None


class PoolStatusResponse(BaseModel):
    name: str
    status: str
    topology: str
    vdevs: List[dict]
    data_vdevs: List[dict] = []
    special_vdevs: List[dict] = []
    log_vdevs: List[dict] = []
    cache_vdevs: List[dict] = []
    scan: dict


@router.get("/", response_model=List[PoolResponse])
async def list_pools(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """List all ZFS pools."""
    return await zfs_manager.list_pools(db)


@router.get("/{pool_name}", response_model=PoolStatusResponse)
async def get_pool_status(
    pool_name: str,
    current_user: dict = Depends(get_current_user)
):
    """Get detailed pool status."""
    return await zfs_manager.get_pool_status(pool_name)


@router.post("/", response_model=PoolResponse)
async def create_pool(
    pool: PoolCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Create a new ZFS pool from inline vdev specs."""
    vdev_dicts = [v.model_dump() for v in pool.vdevs]
    return await zfs_manager.create_pool(
        db,
        name=pool.name,
        vdevs=vdev_dicts,
        ashift=pool.ashift
    )


@router.post("/{pool_name}/scrub")
async def start_scrob(
    pool_name: str,
    current_user: dict = Depends(get_current_user)
):
    """Start a scrub on a pool."""
    await zfs_manager.scrub_pool(pool_name)
    return {"message": f"Scrub started on pool {pool_name}"}


@router.post("/{pool_name}/export")
async def export_pool(
    pool_name: str,
    current_user: dict = Depends(get_current_user)
):
    """Export a pool."""
    await zfs_manager.export_pool(pool_name)
    return {"message": f"Pool {pool_name} exported"}


@router.post("/{pool_name}/import")
async def import_pool(
    pool_name: str,
    current_user: dict = Depends(get_current_user)
):
    """Import a pool."""
    await zfs_manager.import_pool(pool_name)
    return {"message": f"Pool {pool_name} imported"}


@router.get("/{pool_name}/destroy-info")
async def get_pool_destroy_info(
    pool_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Get info shown in the pool-destroy confirmation (space + NFS impact)."""
    return await zfs_manager.get_pool_destroy_info(db, pool_name)


@router.delete("/{pool_name}")
async def destroy_pool(
    pool_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Destroy a pool (DESTRUCTIVE)."""
    await zfs_manager.destroy_pool(db, pool_name)
    return {"message": f"Pool {pool_name} destroyed"}


@router.delete("/{pool_name}/devices/{device_path:path}")
async def remove_device(
    pool_name: str,
    device_path: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Remove a device from a pool."""
    return await zfs_manager.remove_device(
        db,
        pool_name=pool_name,
        device_path=device_path
    )
