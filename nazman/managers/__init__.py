"""Manager classes (business logic per domain).

No instances are created at import time; the application object graph is
assembled in :mod:`nazman.wiring`.
"""

from .disk_manager import DiskManager
from .zfs_manager import ZfsManager
from .snapshot_manager import SnapshotManager
from .nfs_manager import NfsManager
from .smb_manager import SmbManager
from .backup_manager import BackupManager
from .zfs_backup_manager import ZfsBackupManager
from .scheduler import SchedulerManager
from .metrics_manager import MetricsManager
from .metrics_store import MetricsStore

__all__ = [
    "DiskManager",
    "ZfsManager",
    "SnapshotManager",
    "NfsManager",
    "SmbManager",
    "BackupManager",
    "ZfsBackupManager",
    "SchedulerManager",
    "MetricsManager",
    "MetricsStore",
]
