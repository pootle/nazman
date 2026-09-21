from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, BigInteger, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BackupDisk(Base):
    """A disk (or slot on a disk) declared and formatted as a ZFS backup target.

    Physical identity lives solely in ``disks`` (by_id/serial).  The device
    path is derived from ``disks.by_id`` + ``partition_number``; capacity and
    availability are computed live by probing, never persisted.
    """
    __tablename__ = "backup_disks"
    __table_args__ = (
        UniqueConstraint("disk_id", name="uq_backup_disk_disk"),
    )

    id = Column(Integer, primary_key=True, index=True)
    disk_id = Column(Integer, ForeignKey("disks.id"), nullable=False, index=True)
    slot_uuid = Column(String, nullable=True, index=True)  # partition slot, None = whole disk
    partition_number = Column(Integer, nullable=False, default=1)  # which -partN carries the backup FS
    label = Column(String, nullable=True)  # user label identifying the physical media
    fs_type = Column(String, default="ext4")
    mount_point = Column(String, nullable=False)  # e.g. /mnt/backup/<fs_uuid>
    fs_uuid = Column(String, nullable=False, unique=True, index=True)  # identity of the backup volume
    unmount_after_backup = Column(Boolean, default=True)  # unmount after each backup/restore
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    disk = relationship("Disk", lazy="joined")


class BackupSchedule(Base):
    """Per-(dataset, disk) backup policy: which disk, full/incr cadence, retention.

    A dataset may be backed up to multiple disks (e.g. grandfather-father-son
    tiers); the pair is unique so the same disk cannot hold two schedules for
    one dataset.
    """
    __tablename__ = "backup_schedules"
    __table_args__ = (
        UniqueConstraint("dataset_name", "backup_disk_id", name="uq_backup_schedule_dataset_disk"),
    )

    id = Column(Integer, primary_key=True, index=True)
    dataset_name = Column(String, nullable=False, index=True)
    backup_disk_id = Column(Integer, ForeignKey("backup_disks.id", ondelete="CASCADE"), nullable=False, index=True)
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
    backup_disk_id = Column(Integer, ForeignKey("backup_disks.id", ondelete="CASCADE"), nullable=False, index=True)
    backup_type = Column(String, nullable=False)  # full | incremental
    stream_file = Column(String, nullable=True)  # path on backup disk, e.g. tank/ds/incr-...zfs.gz
    snapshot = Column(String, nullable=True)  # ZFS snapshot sent, e.g. tank/ds@backup-...
    base_snapshot = Column(String, nullable=True)  # anchor for incremental
    full_anchor = Column(String, nullable=True)  # full snapshot this chain derives from
    size_bytes = Column(BigInteger, default=0)  # stream file size (compressed)
    changed_bytes = Column(BigInteger, default=0)  # incremental size = changed data
    sha256 = Column(String, nullable=True)  # checksum of the compressed stream file
    phase = Column(String, nullable=True)  # pending | snapshotting | sending | pruning
    status = Column(String, default="running")  # running | success | failed | skipped
    error = Column(String, nullable=True)
    started_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
