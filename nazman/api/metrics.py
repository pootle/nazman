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
from ..managers.metrics_manager import metrics_manager, get_disk_series_names
from ..managers.metrics_store import SYSTEM_POOL
from ..managers import metrics_store as _metrics_store
from ..models.pool import Pool
from ..utils.validation import validate_pool_name

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


def _store():
    return _metrics_store.metrics_store


def _pool_names(db: Session) -> list:
    try:
        return [p.name for p in db.query(Pool).all()]
    except Exception:
        return []


@router.get("/summary")
async def get_monitoring_summary(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Live overview: cpu/memory/net latest + in-memory series + per-pool disks."""
    try:
        series_names = get_disk_series_names()
        pools: dict = {}
        # Provide pool->disks for all configured pools so the monitoring page can
        # render per-pool disk charts even when logging is off.
        from ..managers.zfs_manager import zfs_manager

        pool_names = _pool_names(db)

        for pool in pool_names:
            try:
                status = await zfs_manager.get_pool_status(pool)
            except Exception:
                pools[pool] = []
                continue
            bases = []
            for group in ("data_vdevs", "special_vdevs", "log_vdevs", "cache_vdevs"):
                for vdev in status.get(group, []):
                    for child in vdev.get("children", []):
                        leaf = child.get("name") or child.get("path") or ""
                        base = _norm(leaf)
                        if base in series_names and base not in bases:
                            bases.append(base)
            pools[pool] = bases

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
            "interfaces": _list_interfaces(),
            "selected_interface": _selected_iface(),
            "logging": _logging_state_dict(db),
        }
    except Exception as e:
        return {"error": str(e)}


def _norm(leaf: str) -> str:
    from ..managers.metrics_manager import normalize_base_name

    return normalize_base_name(leaf)


def _list_interfaces():
    from ..managers.metrics_manager import list_network_interfaces

    return list_network_interfaces()


def _selected_iface():
    from ..managers.metrics_manager import get_selected_network_interface

    return get_selected_network_interface()


@router.get("/logging")
async def get_logging_state(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Return per-pool logging state plus general information."""
    return _logging_state_dict(db)


@router.post("/logging")
async def set_logging_state(
    pool: str = Query(...),
    enabled: bool = Query(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Enable/disable disk metrics logging for a single pool."""
    try:
        validate_pool_name(pool)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store = _store()
    store.set_pool_enabled(pool, enabled)

    # Mirror the intent to the app config as the global default so it is visible
    # in settings and survives as a fallback. Per-pool state lives in SQLite.
    any_enabled = bool(store.list_enabled_pools())
    try:
        set_setting("metrics_log_enabled", any_enabled)
    except Exception:
        pass

    return _logging_state_dict(db)


@router.get("/history")
async def get_history(
    pool: str = Query(...),
    metric: str = Query("disk", pattern="^(cpu|memory|net|disk)$"),
    device: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=30),
    current_user: dict = Depends(get_current_user),
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
    store = _store()

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


def _logging_state_dict(db: Session) -> dict:
    store = _store()
    enabled_map = {p: store.is_pool_enabled(p)
                   for p in store.list_enabled_pools()}
    # Report full per-pool state, including configured pools that are off.
    for p in _pool_names(db):
        enabled_map.setdefault(p, False)

    return {
        "enabled": enabled_map,
        "retention_days": get_settings().metrics_log_retention_days,
        "sample_interval": get_settings().monitoring_refresh_interval,
        "db_size_bytes": store.db_size_bytes(),
        "oldest_ts": store.oldest_ts(),
    }