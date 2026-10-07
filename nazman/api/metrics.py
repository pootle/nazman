"""API endpoints for persistent per-pool performance monitoring.

Exposes live metrics (reusing the in-memory metrics manager), per-pool logging
state, and historical samples from the metrics store (disk-backed).
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from ..auth import get_current_user
from ..config import get_settings, set_setting
from ..database import get_db
from ..managers.metrics_manager import (
    MetricsManager,
    list_network_interfaces,
    get_selected_network_interface,
)
from ..managers.metrics_store import MetricsStore, SYSTEM_POOL
from ..managers.zfs_manager import ZfsManager
from ..utils import zfs_query
from ..utils.validation import validate_pool_name
from ..wiring import get_metrics_manager, get_metrics_store, get_zfs_manager

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"], dependencies=[Depends(get_current_user)])


def _pool_names(db: Session, zfs_manager: ZfsManager) -> list:
    try:
        return zfs_manager.list_pool_names(db)
    except Exception:
        return []


@router.get("/summary")
async def get_monitoring_summary(
    db: Session = Depends(get_db),
    metrics_manager: MetricsManager = Depends(get_metrics_manager),
    store: MetricsStore = Depends(get_metrics_store),
    zfs_manager: ZfsManager = Depends(get_zfs_manager),

):
    """Live overview: cpu/memory/net latest + in-memory series + per-pool disks."""
    try:
        series_names = metrics_manager.disk_series_names()
        pools: dict = {}
        # Provide pool->disks for all configured pools so the monitoring page can
        # render per-pool disk charts even when logging is off.
        pool_names = _pool_names(db, zfs_manager)

        for pool in pool_names:
            try:
                status = await zfs_manager.get_pool_status(pool)
            except Exception:
                pools[pool] = []
                continue
            pools[pool] = zfs_query.pool_vdev_bases(status, list(series_names))

        return {
            "timestamp": time.time(),
            "cpu": metrics_manager.get_series("cpu"),
            "memory": metrics_manager.get_series("memory"),
            "net": metrics_manager.get_series("net"),
            "disks": {
                base: metrics_manager.get_series(series)
                for base, series in series_names.items()
            },
            "pools": pools,
            "interfaces": list_network_interfaces(),
            "selected_interface": get_selected_network_interface(),
            "logging": _logging_state_dict(db, store, zfs_manager),
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/logging")
async def get_logging_state(
    db: Session = Depends(get_db),
    store: MetricsStore = Depends(get_metrics_store),
    zfs_manager: ZfsManager = Depends(get_zfs_manager),

):
    """Return per-pool logging state plus general information."""
    return _logging_state_dict(db, store, zfs_manager)


@router.post("/logging")
async def set_logging_state(
    pool: str = Query(...),
    enabled: bool = Query(...),
    db: Session = Depends(get_db),
    store: MetricsStore = Depends(get_metrics_store),
    zfs_manager: ZfsManager = Depends(get_zfs_manager),

):
    """Enable/disable disk metrics logging for a single pool."""
    try:
        validate_pool_name(pool)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store.set_pool_enabled(pool, enabled)

    # Mirror the intent to the app config as the global default so it is visible
    # in settings and survives as a fallback. Per-pool state lives in SQLite.
    any_enabled = bool(store.list_enabled_pools())
    try:
        set_setting("metrics_log_enabled", any_enabled)
    except Exception:
        pass

    return _logging_state_dict(db, store, zfs_manager)


@router.get("/history")
async def get_history(
    pool: str = Query(...),
    metric: str = Query("disk", pattern="^(cpu|memory|net|disk)$"),
    device: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=30),
    store: MetricsStore = Depends(get_metrics_store),

):
    """Return historical samples for a pool from the metrics store.

    ``metric=cpu|memory|net`` returns system series (stored under SYSTEM_POOL).
    ``metric=disk`` returns per-device busy% series for the given pool.
    """
    try:
        validate_pool_name(pool)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    if metric in ("cpu", "memory", "net"):
        series = store.query_metric(SYSTEM_POOL, metric, "<system>",
                                    start_ts, end_ts)
        return {
            "pool": pool,
            "metric": metric,
            "days": days,
            "series": series,
            "devices": [],
        }

    # disk metric: return each recorded device as a named series
    devices = store.distinct_devices(pool, "disk")
    if device:
        devices = [device] if device in devices else []
    named = {}
    for dev in devices:
        named[dev] = store.query_metric(pool, "disk", dev, start_ts, end_ts)
    return {
        "pool": pool,
        "metric": "disk",
        "days": days,
        "series": named,
        "devices": devices,
    }


def _logging_state_dict(
    db: Session, store: MetricsStore, zfs_manager: ZfsManager
) -> dict:
    enabled_map = {p: store.is_pool_enabled(p)
                   for p in store.list_enabled_pools()}
    # Report full per-pool state, including configured pools that are off.
    for p in _pool_names(db, zfs_manager):
        enabled_map.setdefault(p, False)

    return {
        "enabled": enabled_map,
        "retention_days": get_settings().metrics_log_retention_days,
        "sample_interval": get_settings().monitoring_refresh_interval,
        "db_size_bytes": store.db_size_bytes(),
        "oldest_ts": store.oldest_ts(),
    }