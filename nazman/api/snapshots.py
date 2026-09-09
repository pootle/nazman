from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..auth import get_current_user
from ..managers import snapshot_manager

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"], dependencies=[Depends(get_current_user)])


class SnapshotCreate(BaseModel):
    dataset_name: str
    snapshot_name: str


class SnapshotResponse(BaseModel):
    name: str
    dataset_name: str
    snapshot_name: str
    used: Optional[str] = None
    referenced: Optional[str] = None
    creation: Optional[str] = None


@router.get("/", response_model=List[SnapshotResponse])
async def list_snapshots(
    dataset_name: Optional[str] = None,
    db: Session = Depends(get_db),

):
    """List all snapshots from ZFS (live query)."""
    return await snapshot_manager.list_snapshots(db, dataset_name)


@router.post("/", response_model=SnapshotResponse)
async def create_snapshot(
    snapshot: SnapshotCreate,
    db: Session = Depends(get_db),

):
    """Create a new snapshot."""
    return await snapshot_manager.create_snapshot(
        db,
        dataset_name=snapshot.dataset_name,
        snapshot_name=snapshot.snapshot_name
    )


@router.delete("/{snapshot_name:path}")
async def destroy_snapshot(
    snapshot_name: str,
    db: Session = Depends(get_db),

):
    """Destroy a snapshot (DESTRUCTIVE)."""
    await snapshot_manager.destroy_snapshot(db, snapshot_name)
    return {"message": f"Snapshot {snapshot_name} destroyed"}
