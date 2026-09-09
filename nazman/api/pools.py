from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_manager
from ..models.pool import Pool

router = APIRouter(prefix="/api/pools", tags=["pools"], dependencies=[Depends(get_current_user)])


class DeviceSpec(BaseModel):
    disk_id: int
    slot_uuid: Optional[str] = None  # None = whole disk


class VdevSpec(BaseModel):
    role: Literal["data", "log", "cache", "special"]
    topology: Literal["stripe", "mirror", "raidz1", "raidz2", "raidz3"]
    devices: List[DeviceSpec]
    ashift: Optional[int] = Field(default=None, ge=9, le=16)


class PoolCreate(BaseModel):
    name: str
    vdevs: List[VdevSpec]
    ashift: int = Field(default=12, ge=9, le=16)


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

):
    """List all ZFS pools."""
    return await zfs_manager.list_pools(db)


@router.get("/{pool_name}", response_model=PoolStatusResponse)
async def get_pool_status(
    pool_name: str,

):
    """Get detailed pool status."""
    return await zfs_manager.get_pool_status(pool_name)


@router.post("/", response_model=PoolResponse)
async def create_pool(
    pool: PoolCreate,
    db: Session = Depends(get_db),

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

):
    """Start a scrub on a pool."""
    await zfs_manager.scrub_pool(pool_name)
    return {"message": f"Scrub started on pool {pool_name}"}


@router.post("/{pool_name}/export")
async def export_pool(
    pool_name: str,

):
    """Export a pool."""
    await zfs_manager.export_pool(pool_name)
    return {"message": f"Pool {pool_name} exported"}


@router.post("/{pool_name}/import")
async def import_pool(
    pool_name: str,

):
    """Import a pool."""
    await zfs_manager.import_pool(pool_name)
    return {"message": f"Pool {pool_name} imported"}


@router.get("/{pool_name}/destroy-info")
async def get_pool_destroy_info(
    pool_name: str,
    db: Session = Depends(get_db),

):
    """Get info shown in the pool-destroy confirmation (space + NFS impact)."""
    return await zfs_manager.get_pool_destroy_info(db, pool_name)


@router.delete("/{pool_name}")
async def destroy_pool(
    pool_name: str,
    db: Session = Depends(get_db),

):
    """Destroy a pool (DESTRUCTIVE)."""
    await zfs_manager.destroy_pool(db, pool_name)
    return {"message": f"Pool {pool_name} destroyed"}


@router.delete("/{pool_name}/devices/{device_path:path}")
async def remove_device(
    pool_name: str,
    device_path: str,
    db: Session = Depends(get_db),

):
    """Remove a device from a pool."""
    return await zfs_manager.remove_device(
        db,
        pool_name=pool_name,
        device_path=device_path
    )
