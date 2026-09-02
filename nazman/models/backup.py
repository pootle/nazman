from sqlalchemy import Column, Integer, String, DateTime, Text
from datetime import datetime, timezone
from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BackupCommit(Base):
    __tablename__ = "backup_commits"
    
    id = Column(Integer, primary_key=True, index=True)
    commit_hash = Column(String, nullable=False, index=True)
    commit_message = Column(String, nullable=False)
    author = Column(String, default="nazman")
    files_changed = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)
