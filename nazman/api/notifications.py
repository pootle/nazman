from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import get_current_user
from ..utils.notification_store import notification_store

router = APIRouter(
    prefix="/api/notifications",
    tags=["notifications"],
    dependencies=[Depends(get_current_user)],
)


class NotificationCreate(BaseModel):
    message: str
    level: str = "info"
    title: Optional[str] = None
    source: Optional[str] = "ui"
    duration_ms: Optional[int] = None
    bytes: Optional[int] = None


class ReadRequest(BaseModel):
    ids: Optional[List[int]] = None


@router.get("", response_model=dict)
async def list_notifications(
    limit: int = Query(100, ge=1, le=500),
    since_id: Optional[int] = Query(None, ge=0),
    unread_only: bool = False,
):
    """Journal entries newest-first, plus the unread count and newest id."""
    entries = notification_store.list(
        limit=limit, since_id=since_id, unread_only=unread_only,
    )
    return {
        "entries": entries,
        "unread": notification_store.unread_count(),
        "max_id": notification_store.max_id(),
    }


@router.post("", response_model=dict)
async def create_notification(payload: NotificationCreate):
    """Record a notification (the UI mirrors its transient toasts here)."""
    new_id = notification_store.add(
        message=payload.message,
        level=payload.level,
        title=payload.title,
        source=payload.source,
        duration_ms=payload.duration_ms,
        bytes=payload.bytes,
    )
    return {"id": new_id, "unread": notification_store.unread_count()}


@router.post("/read", response_model=dict)
async def mark_read(payload: ReadRequest):
    """Mark the given ids (or every entry when ``ids`` is omitted) as read."""
    notification_store.mark_read(payload.ids)
    return {"unread": notification_store.unread_count()}


@router.delete("", response_model=dict)
async def clear_notifications():
    notification_store.clear()
    return {"unread": 0}