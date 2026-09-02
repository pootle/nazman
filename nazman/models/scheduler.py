from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, JSON
from datetime import datetime, timezone
import enum
from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskType(str, enum.Enum):
    SCRUB = "scrub"
    SNAPSHOT = "snapshot"
    BACKUP = "backup"
    ZFS_BACKUP = "zfs_backup"
    HEALTH_CHECK = "health_check"


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    task_type = Column(String, nullable=False)  # TaskType enum value
    target = Column(String, nullable=False)  # pool name, dataset name, etc.
    schedule = Column(String, nullable=False)  # cron-like: "0 2 * * 0" for weekly at 2am
    enabled = Column(Boolean, default=True)
    config = Column(JSON, nullable=True)  # Task-specific configuration
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class TaskHistory(Base):
    __tablename__ = "task_history"
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("scheduled_tasks.id"), nullable=False)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=False)  # running, success, failed
    output = Column(String, nullable=True)
    error = Column(String, nullable=True)
