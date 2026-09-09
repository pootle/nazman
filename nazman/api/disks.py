from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import re
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import disk_manager, zfs_manager
from ..managers.disk_manager import (
    read_slot_uuids, partition_by_id,
    get_device_entry, get_device_path, get_device_name,
    get_os_reserved_partition_names,
)
from ..models.disk import Disk
from ..utils.exceptions import DiskError


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

    model_config = {"from_attributes": True}

    @classmethod
    def from_disk(cls, disk: Disk) -> "DiskResponse":
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


class BatchPartitionRequest(BaseModel):
    disk_ids: List[int]
    partitions: List[PartitionRequest]


@router.get("/", response_model=List[DiskResponse])
async def list_disks(
    db: Session = Depends(get_db),

):
    """List all discovered disks."""
    disks = await disk_manager.sync_disks_to_database(db)
    return [DiskResponse.from_disk(d) for d in disks]


@router.get("/{disk_id}", response_model=DiskResponse)
async def get_disk(
    disk_id: int,
    db: Session = Depends(get_db),

):
    """Get disk by ID."""
    disk = db.query(Disk).filter(Disk.id == disk_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    return DiskResponse.from_disk(disk)


@router.get("/{disk_id}/health")
async def get_disk_health(
    disk_id: int,
    db: Session = Depends(get_db),

):
    """Get disk health information."""
    disk = db.query(Disk).filter(Disk.id == disk_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")

    try:
        device_path = disk_manager._live_device_path(disk, action="read SMART health")
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    health = await disk_manager.get_disk_health(device_path)
    return health


@router.get("/{disk_id}/partitions", response_model=DiskPartitionsResponse)
async def get_disk_partitions(
    disk_id: int,
    db: Session = Depends(get_db),

):
    """Read partitions from disk (reads GPT names, not DB)."""
    disk = db.query(Disk).filter(Disk.id == disk_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")

    try:
        device_path = disk_manager._live_device_path(disk, action="read partitions")
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    slot_info = await read_slot_uuids([device_path])
    disk_parts = slot_info.get(device_path, {}).get("partitions", [])

    reserved_names = await get_os_reserved_partition_names()

    partitions = []
    for part in disk_parts:
        part_name = part["name"]
        m = re.search(r'(\d+)$', part_name)
        if not m:
            continue
        part_num = int(m.group(1))

        slot_uuid = part.get("slot_uuid")
        if not slot_uuid:
            continue
        dev_path = partition_by_id(disk.by_id, part_num) or part_name

        partitions.append(PartitionSlot(
            number=part_num,
            slot_uuid=slot_uuid,
            device_path=dev_path,
            size_bytes=part.get("size_bytes", 0),
            reserved=part_name in reserved_names,
        ))

    return DiskPartitionsResponse(
        disk_id=disk.id,
        disk_name=get_device_name(disk) or disk.model or disk.serial,
        partitions=partitions,
    )


@router.post("/{disk_id}/wipe")
async def wipe_disk(
    disk_id: int,
    db: Session = Depends(get_db),

):
    """Wipe all partition tables from a disk."""
    try:
        return await disk_manager.wipe_disk(db, disk_id)
    except DiskError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{disk_id}/partition", response_model=DiskPartitionsResponse)
async def partition_disk(
    disk_id: int,
    request: PartitionDiskRequest,
    db: Session = Depends(get_db),

):
    """Partition a disk. Generates slot UUIDs and writes them to GPT names."""
    try:
        await disk_manager.partition_disk(
            db, disk_id, [p.model_dump() for p in request.partitions]
        )
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Read back the result
    return await get_disk_partitions(disk_id, db)


@router.post("/batch-partition")
async def batch_partition_disks(
    request: BatchPartitionRequest,
    db: Session = Depends(get_db),

):
    """Apply the same partition layout to multiple disks."""
    if not request.partitions:
        raise HTTPException(status_code=400, detail="At least one partition required")

    results = []
    for disk_id in request.disk_ids:
        disk = db.query(Disk).filter(Disk.id == disk_id).first()
        if not disk:
            results.append({"disk_id": disk_id, "success": False, "error": "Disk not found"})
            continue
        if disk.is_os_disk:
            results.append({"disk_id": disk_id, "success": False, "error": "Cannot modify OS disk"})
            continue
        try:
            result = await disk_manager.partition_disk(
                db, disk_id, [p.model_dump() for p in request.partitions]
            )
            results.append(result)
        except Exception as e:
            results.append({"disk_id": disk_id, "success": False, "error": str(e)})

    return results


@router.post("/batch-wipe")
async def batch_wipe_disks(
    request: BatchPartitionRequest,
    db: Session = Depends(get_db),

):
    """Wipe partition tables from multiple disks (no new partitions created)."""
    return await disk_manager.batch_wipe_disks(db, request.disk_ids)


@router.patch("/{disk_id}")
async def update_disk(
    disk_id: int,
    updates: dict,
    db: Session = Depends(get_db),

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

):
    """Permanently remove a disk row that is no longer present.

    Refuses to drop a disk that is currently attached, since that could purge
    knowledge of a live device. Use to clean up stale rows (e.g. pulled in
    from another machine) for disks that no longer exist in the system.
    """
    try:
        return await disk_manager.drop_disk(db, disk_id)
    except DiskError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{disk_id}/secure-wipe")
async def secure_wipe_disk(
    disk_id: int,
    db: Session = Depends(get_db),

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

):
    """Reactivate a dead disk."""
    try:
        return await disk_manager.resurrect_disk(db, disk_id)
    except DiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
