from sqlalchemy import Column, Integer, String, DateTime, Boolean
from datetime import datetime, timezone
from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Disk(Base):
    """A physical disk, identified by its stable by-id (or serial) identity.

    ``device_name``/``device_path`` are intentionally NOT persisted here: they
    are ephemeral kernel names (/dev/sdX) that can change on boot or hot-plug.
    Identity is keyed by ``by_id`` (unique); ``serial`` is the stable fallback
    for disks without a by-id symlink.  The live kernel path of a present disk
    is held in the in-memory device map (see DiskManager).
    """

    __tablename__ = "disks"

    id = Column(Integer, primary_key=True, index=True)
    by_id = Column(String, unique=True, nullable=False, index=True)
    model = Column(String, nullable=True)
    serial = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=False)
    disk_type = Column(String, nullable=False)  # ssd, hdd, nvme
    rotation_speed = Column(Integer, nullable=True)
    health_status = Column(String, default="unknown")
    is_os_disk = Column(Boolean, default=False)
    status = Column(String, default="active")  # active, dead, removed
    temperature = Column(Integer, nullable=True)
    power_on_hours = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
