from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers.disk_manager import DiskManager
from ..models.disk import Disk
from ..services.disk_view import DiskViewService
from ..utils.exceptions import DiskError, NotFoundError
from ..wiring import get_disk_manager, get_disk_view_service

router = APIRouter(prefix="/api/disks", tags=["disks"], dependencies=[Depends(get_current_user)])


class DiskResponse(BaseModel):
    id: int
    by_id: Optional[str] = None
    model: Optional[str]
    serial: Optional[str]
    size_bytes: int
    disk_type: str
    health_status: str
    temperature: Optional[int]
    is_os_disk: bool = False
    status: str = "active"
    device_name: Optional[str] = None
    device_path: Optional[str] = None
    partition_count: int = 0
    free_percent: Optional[int] = None
    role: str = "unused"
    role_detail: Optional[str] = None
    pools: List[str] = []
    backup_state: Optional[str] = None
    zfs_errors: Optional[dict] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_view(cls, view: dict) -> "DiskResponse":
        disk = view["disk"]
        return cls(
            id=disk.id,
            by_id=disk.by_id,
            model=disk.model,
            serial=disk.serial,
            size_bytes=disk.size_bytes,
            disk_type=disk.disk_type,
            health_status=disk.health_status,
            temperature=disk.temperature,
            is_os_disk=disk.is_os_disk,
            status=disk.status,
            device_name=view.get("device_name"),
            device_path=view.get("device_path"),
            partition_count=view["partition_count"],
            free_percent=view["free_percent"],
            role=view["role"],
            role_detail=view["role_detail"],
            pools=view.get("pools") or [],
            backup_state=view["backup_state"],
            zfs_errors=view["zfs_errors"],
        )

    @classmethod
    def from_disk(cls, disk: Disk) -> "DiskResponse":
        from ..utils.devices import get_device_entry, get_device_name, get_device_path
        entry = get_device_entry(disk) or {}
        return cls(
            id=disk.id,
            by_id=disk.by_id,
            model=disk.model,
            serial=disk.serial,
            size_bytes=disk.size_bytes,
            disk_type=disk.disk_type,
            health_status=disk.health_status,
            temperature=disk.temperature,
            is_os_disk=disk.is_os_disk,
            status=disk.status,
            device_name=entry.get("device_name") or get_device_name(disk),
            device_path=entry.get("device_path") or get_device_path(disk),
        )


class PartitionSlot(BaseModel):
    number: int
    slot_uuid: str
    device_path: str  # by-id path
    size_bytes: int
    reserved: bool = False  # True if the partition is used by the OS (root/boot/md)


class DiskPartitionsResponse(BaseModel):
    disk_id: int
    disk_name: str
    partitions: List[PartitionSlot]


class PartitionRequest(BaseModel):
    """Spec for one partition to create."""
    size_mb: Optional[int] = None  # None = rest of disk


class PartitionDiskRequest(BaseModel):
    partitions: List[PartitionRequest]


class RecreatePartitionRequest(BaseModel):
    size_mb: Optional[int] = None  # None = rest of disk
    slot_uuid: Optional[str] = None  # recorded nazman:<uuid>; generated if absent


class RecreatePartitionsRequest(BaseModel):
    partitions: List[RecreatePartitionRequest]


class BatchPartitionRequest(BaseModel):
    disk_ids: List[int]
    partitions: List[PartitionRequest]


@router.get("/", response_model=List[DiskResponse])
async def list_disks(
    db: Session = Depends(get_db),
    disk_view: DiskViewService = Depends(get_disk_view_service),

):
    """List all discovered disks."""
    views = await disk_view.list_disks(db)
    return [DiskResponse.from_view(v) for v in views]


@router.get("/{disk_id}", response_model=DiskResponse)
async def get_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_view: DiskViewService = Depends(get_disk_view_service),

):
    """Get disk by ID."""
    return DiskResponse.from_view(await disk_view.get_disk(db, disk_id))


@router.get("/{disk_id}/health")
async def get_disk_health(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_view: DiskViewService = Depends(get_disk_view_service),

):
    """SMART + ZFS integrity details for one disk (details modal payload).

    ``smart`` is null when the disk is not present or SMART is unavailable;
    ``zfs`` is null/empty for disks not owned by a pool.  ZFS event history is
    whatever ``zpool events`` still holds in its recent ring buffer.
    """
    detail = await disk_view.health_detail(db, disk_id)
    return {
        "disk": DiskResponse.from_view(detail["view"]),
        "smart": detail["smart"],
        "zfs": detail["zfs"],
    }


@router.get("/{disk_id}/partitions", response_model=DiskPartitionsResponse)
async def get_disk_partitions(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_view: DiskViewService = Depends(get_disk_view_service),

):
    """Read partitions from disk (reads GPT names, not DB)."""
    try:
        result = await disk_view.partitions(db, disk_id)
    except NotFoundError:
        raise
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DiskPartitionsResponse(**result)


@router.post("/{disk_id}/wipe")
async def wipe_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Wipe all partition tables from a disk."""
    return await disk_manager.wipe_disk(db, disk_id)


@router.post("/{disk_id}/partition", response_model=DiskPartitionsResponse)
async def partition_disk(
    disk_id: int,
    request: PartitionDiskRequest,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),
    disk_view: DiskViewService = Depends(get_disk_view_service),

):
    """Partition a disk. Generates slot UUIDs and writes them to GPT names."""
    try:
        await disk_manager.partition_disk(
            db, disk_id, [p.model_dump() for p in request.partitions]
        )
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Read back the result
    return DiskPartitionsResponse(**await disk_view.partitions(db, disk_id))


@router.post("/{disk_id}/recreate-partitions")
async def recreate_partitions(
    disk_id: int,
    request: RecreatePartitionsRequest,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),
):
    """Recreate a GPT layout, preserving recorded ``nazman:<uuid>`` slot labels.

    Used during a system rebuild so partition-based pool vdevs resolve to the
    same slots recorded in the backup manifest.  Destructive: wipes the disk.
    """
    try:
        return await disk_manager.recreate_partition_layout(
            db, disk_id, [p.model_dump() for p in request.partitions]
        )
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/batch-partition")
async def batch_partition_disks(
    request: BatchPartitionRequest,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Apply the same partition layout to multiple disks."""
    if not request.partitions:
        raise HTTPException(status_code=400, detail="At least one partition required")

    results = []
    for disk_id in request.disk_ids:
        try:
            result = await disk_manager.partition_disk(
                db, disk_id, [p.model_dump() for p in request.partitions]
            )
            results.append(result)
        except NotFoundError as e:
            results.append({"disk_id": disk_id, "success": False, "error": str(e)})
        except Exception as e:
            results.append({"disk_id": disk_id, "success": False, "error": str(e)})

    return results


@router.post("/batch-wipe")
async def batch_wipe_disks(
    request: BatchPartitionRequest,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Wipe partition tables from multiple disks (no new partitions created)."""
    return await disk_manager.batch_wipe_disks(db, request.disk_ids)


@router.patch("/{disk_id}")
async def update_disk(
    disk_id: int,
    updates: dict,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Update disk fields (status, etc.)."""
    try:
        disk = await disk_manager.update_disk(db, disk_id, updates)
        return DiskResponse.from_disk(disk)
    except DiskError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{disk_id}")
async def drop_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Permanently remove a disk row that is no longer present.

    Refuses to drop a disk that is currently attached, since that could purge
    knowledge of a live device. Use to clean up stale rows (e.g. pulled in
    from another machine) for disks that no longer exist in the system.
    """
    return await disk_manager.drop_disk(db, disk_id)


@router.post("/{disk_id}/secure-wipe")
async def secure_wipe_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Securely wipe a disk using media-appropriate method."""
    try:
        return await disk_manager.secure_wipe_disk(db, disk_id)
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{disk_id}/resurrect")
async def resurrect_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    disk_manager: DiskManager = Depends(get_disk_manager),

):
    """Reactivate a dead disk."""
    return await disk_manager.resurrect_disk(db, disk_id)
