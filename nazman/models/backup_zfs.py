from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, BigInteger
from datetime import datetime, timezone
from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BackupDisk(Base):
    """A physical disk declared and formatted by the user as a ZFS backup target."""
    __tablename__ = "backup_disks"

    id = Column(Integer, primary_key=True, index=True)
    disk_id = Column(Integer, ForeignKey("disks.id"), nullable=False, index=True)
    device_path = Column(String, nullable=False)  # by-id path of the whole disk
    fs_type = Column(String, default="ext4")
    mount_point = Column(String, nullable=False)  # e.g. /mnt/backup/<fs_uuid>
    fs_uuid = Column(String, nullable=False, index=True)  # ext4 filesystem UUID
    total_bytes = Column(BigInteger, default=0)
    free_bytes = Column(BigInteger, default=0)
    status = Column(String, default="mounted")  # mounted, unmounted, full, error
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class BackupSchedule(Base):
    """Per-dataset backup policy: which disk, full/incr cadence, retention."""
    __tablename__ = "backup_schedules"

    id = Column(Integer, primary_key=True, index=True)
    dataset_name = Column(String, nullable=False, index=True)
    backup_disk_id = Column(Integer, ForeignKey("backup_disks.id"), nullable=False, index=True)
    full_cron = Column(String, nullable=True)
    incremental_cron = Column(String, nullable=True)
    full_retention = Column(Integer, default=3)
    incremental_retention = Column(Integer, default=7)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class BackupRun(Base):
    """One executed ZFS backup (full or incremental) of a dataset to a backup disk."""
    __tablename__ = "backup_runs"

    id = Column(Integer, primary_key=True, index=True)
    dataset_name = Column(String, nullable=False, index=True)  # ZFS dataset name, e.g. tank/data
    backup_disk_id = Column(Integer, ForeignKey("backup_disks.id"), nullable=False, index=True)
    backup_type = Column(String, nullable=False)  # full | incremental
    stream_file = Column(String, nullable=False)  # path on backup disk, e.g. tank/ds/incr-...zfs.gz
    snapshot = Column(String, nullable=False)  # ZFS snapshot sent, e.g. tank/ds@backup-...
    base_snapshot = Column(String, nullable=True)  # anchor for incremental
    full_anchor = Column(String, nullable=True)  # full snapshot this chain derives from
    size_bytes = Column(BigInteger, default=0)  # stream file size (compressed)
    changed_bytes = Column(BigInteger, default=0)  # incremental size = changed data
    status = Column(String, default="running")  # running | success | failed
    error = Column(String, nullable=True)
    started_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
