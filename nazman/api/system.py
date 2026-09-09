from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import psutil
import asyncio

from ..database import get_db
from ..auth import get_current_user
from ..managers import zfs_manager, disk_manager, metrics_manager
from ..config import get_settings
from ..utils.command_log import command_log
from ..utils.command_log_store import command_log_store
from ..utils.command_tags import VALID_OPS, VALID_STATUSES
from ..models.pool import Pool
from ..managers.metrics_manager import (
    list_network_interfaces,
    get_selected_network_interface,
    get_disk_series_names,
    normalize_base_name,
)

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(get_current_user)])


class SystemInfo(BaseModel):
    cpu_percent: float
    memory: Dict[str, Any]
    disk: Dict[str, Any]


class StorageInfo(BaseModel):
    pool_count: int
    disk_count: int


class SystemStatusResponse(BaseModel):
    timestamp: str
    system: SystemInfo
    storage: StorageInfo


class CommandLogEntry(BaseModel):
    ts: str
    command: str
    status: str
    op: Optional[str] = None
    category: Optional[str] = None
    returncode: Optional[int] = None
    stderr: Optional[str] = None
    duration_ms: Optional[int] = None


class CommandLogResponse(BaseModel):
    entries: List[CommandLogEntry]
    size: int
    total: int

# Health check is unauthenticated for load balancers/monitors
health_router = APIRouter(tags=["system"])

@health_router.get("/api/system/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(
    db: Session = Depends(get_db),

):
    """Get system status overview."""
    try:
        # Get CPU usage (blocking call with interval=1)
        cpu_percent = await asyncio.to_thread(psutil.cpu_percent, interval=1)
        
        # Get memory usage
        memory = await asyncio.to_thread(psutil.virtual_memory)
        
        # Get disk usage for OS partition
        disk_usage = await asyncio.to_thread(psutil.disk_usage, '/')
        
        # Get pool status
        pools = await zfs_manager.list_pools(db)
        
        # Get disk count
        disks = await disk_manager.sync_disks_to_database(db)
        
        return SystemStatusResponse(
            timestamp=datetime.now(timezone.utc).isoformat(),
            system=SystemInfo(
                cpu_percent=cpu_percent,
                memory={
                    "total": memory.total,
                    "available": memory.available,
                    "percent": memory.percent
                },
                disk={
                    "total": disk_usage.total,
                    "used": disk_usage.used,
                    "free": disk_usage.free,
                    "percent": disk_usage.percent
                }
            ),
            storage=StorageInfo(
                pool_count=len(pools),
                disk_count=len(disks)
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metrics")
async def get_system_metrics(
    db: Session = Depends(get_db),

):
    """Metrics for dashboard graphs: full recorded history + current values."""
    try:
        cpu_series = metrics_manager.get_series("cpu")
        mem_series = metrics_manager.get_series("memory")
        net_series = metrics_manager.get_series("net")

        # Per-disk series + map to base device names
        disk_series_names = get_disk_series_names()
        disks = {}
        for base, series in disk_series_names.items():
            disks[base] = metrics_manager.get_series(series)

        # Interfaces available for selection
        interfaces = list_network_interfaces()

        # Map each pool to its disk base names, prioritised: data vdev disks
        # first, then special vdev disks, then the rest (log/cache).  Capped to
        # keep the dashboard minicard readable.
        def _collect_pool_bases(status, disk_series_names, limit=4):
            bases = []
            groups = ["data_vdevs", "special_vdevs", "log_vdevs", "cache_vdevs"]
            for group in groups:
                for vdev in status.get(group, []):
                    for child in vdev.get("children", []):
                        leaf = child.get("name") or child.get("path") or ""
                        base = normalize_base_name(leaf)
                        if base in disk_series_names and base not in bases:
                            bases.append(base)
                        if len(bases) >= limit:
                            return bases[:limit]
            return bases[:limit]

        pools_map = {}
        pools_db = db.query(Pool).all() if db else []
        for pool in pools_db:
            try:
                status = await zfs_manager.get_pool_status(pool.name)
                pools_map[pool.name] = _collect_pool_bases(status, disk_series_names)
            except Exception:
                pools_map[pool.name] = []

        memory = await asyncio.to_thread(psutil.virtual_memory)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cpu": cpu_series,
            "memory": {
                "total": memory.total,
                "used": memory.used,
                "percent": memory.percent,
            },
            "net": net_series,
            "disks": disks,
            "pools": pools_map,
            "interfaces": interfaces,
            "selected_interface": get_selected_network_interface(),
            "history": {
                "cpu": cpu_series,
                "memory": mem_series,
            },
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/command-log", response_model=CommandLogResponse)
async def get_command_log(
    type: str | None = Query(
        None,
        description="Filter by operation type: read, write, system. Comma-separated to combine.",
    ),
    status: str | None = Query(
        None,
        description="Filter by outcome status: success, failed, timeout, error. Comma-separated to combine.",
    ),

):
    """Return recent command executions (newest first).

    Entries may be filtered by ``type`` (read/write/system) and ``status``
    (success/failed/timeout/error); pass comma-separated values to combine.
    """
    settings = get_settings()

    entry_ops = None
    if type is not None:
        entry_ops = [t.strip() for t in type.split(",") if t.strip()]
        invalid = [t for t in entry_ops if t not in VALID_OPS]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid type filter: {', '.join(invalid)}. "
                    f"Valid values: {' ,'.join(sorted(VALID_OPS))}."
                ),
            )

    entry_statuses = None
    if status is not None:
        entry_statuses = [s.strip() for s in status.split(",") if s.strip()]
        invalid = [s for s in entry_statuses if s not in VALID_STATUSES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid status filter: {', '.join(invalid)}. "
                    f"Valid values: {' ,'.join(sorted(VALID_STATUSES))}."
                ),
            )

    entries = command_log.get_entries(
        ops=entry_ops,
        statuses=entry_statuses,
        limit=settings.command_log_size,
    )

    return CommandLogResponse(
        entries=entries,
        size=settings.command_log_size,
        total=command_log_store.raw_count,
    )
