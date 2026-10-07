from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, BigInteger, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BackupGroup(Base):
    """A rotation scheme: a fixed list of datasets plus the backup sets that
    are cycled through when backing them up.

    Triggers (cron or the Full/Incremental buttons) always target
    ``active_set_id``.  After a session completes, ``active_set_id`` moves to
    the next set in the cycle, so the following trigger starts there.

    ``active_set_id`` is a plain integer rather than a foreign key: the
    reference is part of a cycle (group -> set -> disk -> group) that SQLite
    cannot express with constraints, and ``ondelete`` semantics for "the active
    set was deleted" are handled explicitly in code.  It is kept honest by
    ``BackupGroupService``, which never leaves it pointing at a removed row.
    """
    __tablename__ = "backup_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, index=True)
    full_cron = Column(String, nullable=True)
    incremental_cron = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    active_set_id = Column(Integer, nullable=True)  # current set in the cycle
    needs_disk = Column(Boolean, default=False)  # last trigger found no usable disk
    copies = Column(Integer, default=1)  # sets updated with each session (redundancy)
    recycle_full_disks = Column(Boolean, default=False)  # wipe a full disk so its chain restarts
    last_session_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    datasets = relationship(
        "BackupGroupDataset", back_populates="group",
        cascade="all, delete-orphan", lazy="selectin",
    )
    sets = relationship(
        "BackupSet", back_populates="group",
        cascade="all, delete-orphan", lazy="selectin",
    )


class BackupGroupDataset(Base):
    """One dataset covered by a backup group.

    Datasets are not duplicated from ZFS: the name is validated against
    ``validate_dataset_name`` on the way in and the set itself remains the
    source of truth, so a destroyed dataset simply stops resolving.
    """
    __tablename__ = "backup_group_datasets"
    __table_args__ = (
        UniqueConstraint("group_id", "dataset_name", name="uq_backup_group_dataset"),
    )

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("backup_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_name = Column(String, nullable=False, index=True)

    group = relationship("BackupGroup", back_populates="datasets")


class BackupSet(Base):
    """One rotation slot in a group's cycle: an ordered chain of disks.

    The disks in a set act as a unit and hold an append-only chain - the first
    visit writes a full backup of every dataset in the group, and later visits
    append incrementals.  When the active disk runs out of space the session
    advances to the next disk in the set, so the base of the chain may sit on
    an earlier disk.  ``active_disk_id`` is a plain integer for the same
    cycle reason as ``BackupGroup.active_set_id``.
    """
    __tablename__ = "backup_sets"
    __table_args__ = (
        UniqueConstraint("group_id", "position", name="uq_backup_set_position"),
    )

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("backup_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False, default=0)  # order within the group's cycle
    label = Column(String, nullable=True)
    active_disk_id = Column(Integer, nullable=True)  # disk currently being written to
    last_used_at = Column(DateTime, nullable=True)  # None until the set's first full
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    group = relationship("BackupGroup", back_populates="sets")
    disks = relationship("BackupDisk", back_populates="backup_set", lazy="selectin")


class BackupSession(Base):
    """One triggered group backup: the unit the UI lists and rotates on.

    A session walks every dataset in the group on the active set's active
    disk, then moves the group to the next set.  Per-dataset detail lives in
    the ``BackupRun`` rows that reference it.
    """
    __tablename__ = "backup_sessions"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("backup_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    backup_set_id = Column(Integer, ForeignKey("backup_sets.id", ondelete="CASCADE"), nullable=True, index=True)
    backup_disk_id = Column(Integer, ForeignKey("backup_disks.id", ondelete="SET NULL"), nullable=True, index=True)
    trigger = Column(String, nullable=False)  # full | incremental
    status = Column(String, default="running")  # running | success | partial | failed | needs_disk
    phase = Column(String, nullable=True)  # resolving | snapshotting | sending | rotating
    datasets_total = Column(Integer, default=0)
    datasets_done = Column(Integer, default=0)
    datasets_skipped = Column(Integer, default=0)
    datasets_failed = Column(Integer, default=0)
    bytes_written = Column(BigInteger, default=0)
    notes = Column(String, nullable=True)  # e.g. a disk was skipped for lack of space
    error = Column(String, nullable=True)
    started_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)

    group = relationship("BackupGroup")
    backup_set = relationship("BackupSet")
    backup_disk = relationship("BackupDisk")
    runs = relationship("BackupRun", back_populates="session", lazy="selectin")


class BackupDisk(Base):
    """A disk (or slot on a disk) declared and formatted as a ZFS backup target.

    Physical identity lives solely in ``disks`` (by_id/serial).  The device
    path is derived from ``disks.by_id`` + ``partition_number``; capacity and
    availability are computed live by probing, never persisted.

    A disk belongs to at most one backup set: a media device holds one chain,
    so it cannot belong to two rotation slots at once.  The unique constraint
    on ``disk_id`` means one physical device yields at most one backup volume.
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
    backup_set_id = Column(Integer, ForeignKey("backup_sets.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    disk = relationship("Disk", lazy="joined")
    backup_set = relationship("BackupSet", back_populates="disks")


class BackupRun(Base):
    """One dataset's stream written by a backup session to one backup disk."""
    __tablename__ = "backup_runs"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("backup_sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    group_id = Column(Integer, ForeignKey("backup_groups.id", ondelete="CASCADE"), nullable=True, index=True)
    backup_set_id = Column(Integer, ForeignKey("backup_sets.id", ondelete="CASCADE"), nullable=True, index=True)
    dataset_name = Column(String, nullable=False, index=True)  # ZFS dataset name, e.g. tank/data
    backup_disk_id = Column(Integer, ForeignKey("backup_disks.id", ondelete="CASCADE"), nullable=False, index=True)
    backup_type = Column(String, nullable=False)  # full | incremental
    promoted_from = Column(String, nullable=True)  # set 'incremental' when promoted to full
    stream_file = Column(String, nullable=True)  # path on backup disk, e.g. tank/ds/incr-...zfs.gz
    snapshot = Column(String, nullable=True)  # ZFS snapshot sent, e.g. tank/ds@backup-...
    base_snapshot = Column(String, nullable=True)  # anchor for incremental
    full_anchor = Column(String, nullable=True)  # full snapshot this chain derives from
    size_bytes = Column(BigInteger, default=0)  # stream file size (compressed)
    estimated_bytes = Column(BigInteger, nullable=True)  # capacity estimate taken before send
    changed_bytes = Column(BigInteger, default=0)  # incremental size = changed data
    sha256 = Column(String, nullable=True)  # checksum of the compressed stream file
    phase = Column(String, nullable=True)  # pending | snapshotting | sending | pruning
    status = Column(String, default="running")  # running | success | failed | skipped
    error = Column(String, nullable=True)
    started_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)

    session = relationship("BackupSession", back_populates="runs")
