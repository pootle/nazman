from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime, timezone
from ..database import Base

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Pool(Base):
    __tablename__ = "pools"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
