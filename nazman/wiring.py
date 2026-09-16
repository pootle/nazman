"""Application object graph: construction and access to managers/services.

Managers are plain classes with no module-level instances; the container is
built lazily once per process (or per test) so nothing with side effects
(threads, schedulers, settings snapshots) happens at import time. Cross-domain
collaborators are wired here via constructor injection, which keeps the
manager modules free of one another.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .managers.disk_manager import DiskManager
from .managers.zfs_manager import ZfsManager
from .managers.nfs_manager import NfsManager
from .managers.smb_manager import SmbManager
from .managers.snapshot_manager import SnapshotManager
from .managers.backup_manager import BackupManager
from .managers.zfs_backup_manager import ZfsBackupManager
from .managers.scheduler import SchedulerManager
from .managers.metrics_manager import MetricsManager, register_default_collectors
from .managers.metrics_store import MetricsStore
from .models.scheduler import TaskType
from .services.destruction import DestructionService
from .services.disk_view import DiskViewService
from .utils.exceptions import NAZManError

logger = logging.getLogger(__name__)


@dataclass
class Container:
    disk: DiskManager
    zfs: ZfsManager
    nfs: NfsManager
    smb: SmbManager
    snapshot: SnapshotManager
    backup: BackupManager
    zfs_backup: ZfsBackupManager
    scheduler: SchedulerManager
    metrics: MetricsManager
    metrics_store: MetricsStore
    destruction: DestructionService
    disk_view: DiskViewService


def build_container() -> Container:
    """Construct the full object graph with collaborators injected."""
    zfs = ZfsManager()
    # Provider is late-bound so ZfsManager patches (tests) take effect.
    disk = DiskManager(pool_members_provider=lambda: zfs.get_pool_members())
    nfs = NfsManager()
    smb = SmbManager()
    snapshot = SnapshotManager()
    backup = BackupManager()
    scheduler = SchedulerManager()
    zfs_backup = ZfsBackupManager(zfs=zfs, scheduler=scheduler)
    metrics_store = MetricsStore()
    metrics = MetricsManager(zfs=zfs, store=metrics_store)
    register_default_collectors(metrics)

    destruction = DestructionService(zfs=zfs, nfs=nfs, smb=smb)
    disk_view = DiskViewService(disk=disk, zfs=zfs, zfs_backup=zfs_backup)

    _register_backup_jobs(scheduler, backup, zfs_backup)

    return Container(
        disk=disk,
        zfs=zfs,
        nfs=nfs,
        smb=smb,
        snapshot=snapshot,
        backup=backup,
        zfs_backup=zfs_backup,
        scheduler=scheduler,
        metrics=metrics,
        metrics_store=metrics_store,
        destruction=destruction,
        disk_view=disk_view,
    )


def _register_backup_jobs(
    scheduler: SchedulerManager,
    backup: BackupManager,
    zfs_backup: ZfsBackupManager,
) -> None:
    """Bind backup task types to scheduler executors (no manager imports below)."""
    dataset_locks: dict = {}

    async def run_config_backup(task, db):
        await backup.backup_configuration(db)

    async def run_zfs_backup(task, db):
        config = task.config or {}
        dataset_name = config.get("dataset_name")
        backup_disk_id = config.get("backup_disk_id")
        backup_type = config.get("type", "full") or "full"
        if not dataset_name or not backup_disk_id:
            raise NAZManError("ZFS backup task requires dataset_name and backup_disk_id")
        # Per-dataset lock to prevent concurrent backups of the same dataset.
        lock = dataset_locks.get(dataset_name)
        if lock is None:
            lock = dataset_locks[dataset_name] = asyncio.Lock()
        async with lock:
            await zfs_backup.run_backup(
                db,
                dataset_name=dataset_name,
                backup_disk_id=backup_disk_id,
                backup_type=backup_type,
            )

    scheduler.register_executor(TaskType.BACKUP.value, run_config_backup)
    scheduler.register_executor(TaskType.ZFS_BACKUP.value, run_zfs_backup)


_container: Optional[Container] = None


def get_container() -> Container:
    """Return the process container, building it lazily on first use."""
    global _container
    if _container is None:
        _container = build_container()
    return _container


def reset_container() -> None:
    """Discard the current container (test isolation; next access rebuilds)."""
    global _container
    _container = None


# ── FastAPI dependency providers ────────────────────────────────────────

def get_zfs_manager() -> ZfsManager:
    return get_container().zfs


def get_disk_manager() -> DiskManager:
    return get_container().disk


def get_nfs_manager() -> NfsManager:
    return get_container().nfs


def get_smb_manager() -> SmbManager:
    return get_container().smb


def get_snapshot_manager() -> SnapshotManager:
    return get_container().snapshot


def get_backup_manager() -> BackupManager:
    return get_container().backup


def get_zfs_backup_manager() -> ZfsBackupManager:
    return get_container().zfs_backup


def get_scheduler_manager() -> SchedulerManager:
    return get_container().scheduler


def get_metrics_manager() -> MetricsManager:
    return get_container().metrics


def get_metrics_store() -> MetricsStore:
    return get_container().metrics_store


def get_destruction_service() -> DestructionService:
    return get_container().destruction


def get_disk_view_service() -> DiskViewService:
    return get_container().disk_view
