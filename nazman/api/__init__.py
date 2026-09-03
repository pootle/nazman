from .disks import router as disks_router
from .pools import router as pools_router
from .datasets import router as datasets_router
from .nfs import router as nfs_router
from .smb import router as smb_router
from .snapshots import router as snapshots_router
from .backup import router as backup_router
from .zfs_backup import router as zfs_backup_router
from .system import router as system_router
from .metrics import router as metrics_router
from .auth import router as auth_router

__all__ = [
    "disks_router",
    "pools_router",
    "datasets_router",
    "nfs_router",
    "smb_router",
    "snapshots_router",
    "backup_router",
    "zfs_backup_router",
    "system_router",
    "metrics_router",
    "auth_router"
]
