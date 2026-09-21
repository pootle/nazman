from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth import get_current_user
from ..config import get_settings, set_setting
from ..managers.alert_manager import AlertManager
from ..utils.exceptions import NAZManError
from ..utils.validation import (
    validate_alert_cooldown,
    validate_alert_threshold,
    validate_telegram_bot_token,
    validate_telegram_chat_id,
)
from ..wiring import get_alert_manager

router = APIRouter(
    prefix="/api/alerts",
    tags=["alerts"],
    dependencies=[Depends(get_current_user)],
)


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 6:
        return "****"
    return token[:3] + "****" + token[-4:]


class AlertConfigResponse(BaseModel):
    enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    pool_usage_threshold: int
    cooldown_minutes: int
    poll_interval: int


class AlertConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    pool_usage_threshold: Optional[int] = None
    cooldown_minutes: Optional[int] = None


class AlertTestRequest(BaseModel):
    message: Optional[str] = None


class AlertHistoryEntry(BaseModel):
    id: int
    event_key: str
    channel: str
    message: str
    severity: str
    delivered: bool
    error: Optional[str] = None
    created_at: Optional[str] = None


@router.get("/config", response_model=AlertConfigResponse)
async def get_alert_config():
    """Return the current alerting configuration (bot token masked)."""
    settings = get_settings()
    return AlertConfigResponse(
        enabled=bool(settings.alerts_enabled),
        telegram_bot_token=_mask_token(settings.telegram_bot_token),
        telegram_chat_id=settings.telegram_chat_id,
        pool_usage_threshold=settings.alert_pool_usage_threshold,
        cooldown_minutes=settings.alert_cooldown_minutes,
        poll_interval=settings.alerts_poll_interval,
    )


@router.put("/config", response_model=AlertConfigResponse)
async def update_alert_config(update: AlertConfigUpdate):
    """Persist alerting configuration via the shared conf file.

    The bot token is only updated when a genuinely new value is supplied;
    sending back the masked token (or an empty string) leaves it unchanged.
    Enabling alerts without a token/chat id configured raises a 400.
    """
    settings = get_settings()
    current_mask = _mask_token(settings.telegram_bot_token)

    try:
        if update.enabled is not None:
            set_setting("alerts_enabled", bool(update.enabled))

        token = (update.telegram_bot_token or "").strip()
        if token and token != current_mask:
            validate_telegram_bot_token(token)
            set_setting("telegram_bot_token", token)

        if update.telegram_chat_id is not None:
            chat_id = validate_telegram_chat_id(update.telegram_chat_id)
            set_setting("telegram_chat_id", chat_id)

        if update.pool_usage_threshold is not None:
            set_setting(
                "alert_pool_usage_threshold",
                validate_alert_threshold(update.pool_usage_threshold),
            )

        if update.cooldown_minutes is not None:
            set_setting(
                "alert_cooldown_minutes",
                validate_alert_cooldown(update.cooldown_minutes),
            )
    except NAZManError as e:
        raise HTTPException(status_code=400, detail=str(e))

    fresh = get_settings()
    enabled_now = bool(fresh.alerts_enabled)
    if enabled_now and (not fresh.telegram_bot_token or not fresh.telegram_chat_id):
        raise HTTPException(
            status_code=400,
            detail="Alerts cannot be enabled without a Telegram bot token and chat id",
        )

    return AlertConfigResponse(
        enabled=enabled_now,
        telegram_bot_token=_mask_token(fresh.telegram_bot_token),
        telegram_chat_id=fresh.telegram_chat_id,
        pool_usage_threshold=fresh.alert_pool_usage_threshold,
        cooldown_minutes=fresh.alert_cooldown_minutes,
        poll_interval=fresh.alerts_poll_interval,
    )


@router.post("/test")
async def send_test_alert(
    payload: AlertTestRequest,
    alert_manager: AlertManager = Depends(get_alert_manager),
):
    """Send a test message now; surfaces Telegram errors to the caller."""
    try:
        await alert_manager.send_test(payload.message)
    except NAZManError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "detail": "Test message sent"}


@router.get("/history", response_model=List[AlertHistoryEntry])
async def get_alert_history(
    limit: int = Query(20, ge=1, le=200),
    alert_manager: AlertManager = Depends(get_alert_manager),
):
    """Most recent alert log entries (newest first)."""
    return await alert_manager.recent(limit)