from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from datetime import datetime, timezone

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlertLog(Base):
    """Record of every notification attempt (delivered or failed).

    Kept for the UI history view so users can confirm alerts fired while they
    were away; rows carry both the channel result and any error description.
    """

    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, index=True)
    event_key = Column(String, nullable=False, index=True)
    channel = Column(String, default="telegram", nullable=False)
    message = Column(Text, nullable=False)
    severity = Column(String, default="error", nullable=False)
    delivered = Column(Boolean, default=False, nullable=False)
    error = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow, index=True)