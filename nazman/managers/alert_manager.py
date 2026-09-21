"""Alert aggregation and Telegram delivery.

AlertManager is a plain class (no module-level state) that directs failure
alerts to the configured Telegram chat with per-event cooldown so a persistently
broken pool does not spam the phone every poll.  It owns a background polling
loop (started/stopped with the app) that reads live pool state straight from
``zpool`` via the audited command wrappers, keeping ZFS the source of truth.

Default non-subscribers (e.g. the scheduler, which injects None when absent)
skip notification entirely, so alerting stays optional.
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional

from ..config import get_settings
from ..utils.commands import run_zpool
from ..utils.exceptions import NAZManError
from ..utils.telegram import send_telegram_message

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = ("info", "warning", "error")


def _scan_error_count(scan: Optional[dict]) -> int:
    """Total recorded errors for a scrub/resilver scan block (0 when none)."""
    if not isinstance(scan, dict):
        return 0
    errors = scan.get("errors", 0)
    if isinstance(errors, dict):
        return sum(int(v or 0) for v in errors.values())
    try:
        return int(errors or 0)
    except (TypeError, ValueError):
        return 0


class AlertManager:
    """Cooldown-gated Telegram alerts driven by the background poll loop."""

    def __init__(self) -> None:
        self._last_sent: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._started = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background poll loop (idempotent; cheap no-op when off)."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the background poll loop (idempotent)."""
        if not self._started:
            return
        self._started = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while self._started:
            interval = get_settings().alerts_poll_interval
            if interval > 0:
                await asyncio.sleep(interval)
            if not self._started:
                break
            try:
                await self._check()
            except Exception:
                logger.exception("Alert poll loop error")

    # ── Public API ──────────────────────────────────────────────────────

    async def notify(
        self,
        event_key: str,
        message: str,
        severity: str = "error",
        force: bool = False,
    ) -> bool:
        """Send ``message`` once per cooldown window for ``event_key``.

        Returns True when a message was delivered.  Never raises: background
        callers (scheduler failures, pool polls) must not surface transient
        Telegram errors into their own failure paths.  The outcome is always
        recorded in the ``alert_log`` table for the UI history view.
        """
        settings = get_settings()
        if not event_key:
            return False
        if not force:
            if not settings.alerts_enabled:
                return False
            if (not settings.telegram_bot_token or not settings.telegram_chat_id):
                logger.warning("Alerts enabled but Telegram not configured")
                return False
            if self._suppressed(event_key, settings.alert_cooldown_minutes):
                return False

        try:
            delivered, error = await self._deliver(message, severity)
        except Exception as e:  # pragma: no cover - defensive
            delivered, error = False, str(e)

        await self._record(event_key, message, severity, delivered, error)
        if delivered:
            self._last_sent[event_key] = time.monotonic()
        return delivered

    async def send_test(self, message: Optional[str] = None) -> bool:
        """Send an unconditional test message, raising on misconfiguration.

        Used by the Settings UI's "Send test" button to validate the token and
        chat id; errors (including Telegram's own description, e.g. an unknown
        chat) propagate so the UI can display them.
        """
        settings = get_settings()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise NAZManError("Telegram bot token and chat id are required")
        text = message or "NAZMan test alert - delivery is working."
        await send_telegram_message(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            text,
        )
        await self._record("test", text, "info", True, None)
        return True

    async def recent(self, limit: int = 20) -> list:
        """The ``limit`` most recent alert log entries (newest first)."""
        from ..database import get_db_context
        from ..models.alert import AlertLog

        with get_db_context() as db:
            rows = (
                db.query(AlertLog)
                .order_by(AlertLog.created_at.desc(), AlertLog.id.desc())
                .limit(max(1, limit))
                .all()
            )
            return [
                {
                    "id": r.id,
                    "event_key": r.event_key,
                    "channel": r.channel,
                    "message": r.message,
                    "severity": r.severity,
                    "delivered": bool(r.delivered),
                    "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    # ── Background poll loop ────────────────────────────────────────────

    async def _check(self) -> None:
        """One poll: alert on pool state, scan errors, and capacity overflow."""
        settings = get_settings()
        if not settings.alerts_enabled:
            return
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return

        try:
            await self._check_pool_status()
            await self._check_pool_capacity(settings.alert_pool_usage_threshold)
        except Exception:
            logger.exception("Pool alert poll failed")

    async def _check_pool_status(self) -> None:
        """Alert on non-ONLINE pool state and scrub/resilver errors."""
        stdout, _, rc = await run_zpool("status", "-j", op="read", check=False)
        if rc != 0:
            return
        data = json.loads(stdout)
        pools = data.get("pools", {}) or {}
        if not isinstance(pools, dict):
            return

        for name, info in pools.items():
            state = (info.get("state") or "ONLINE").upper()
            if state != "ONLINE":
                await self.notify(
                    f"pool:{name}:state",
                    f"Pool {name} is {state}",
                    severity="error",
                )
            errors = _scan_error_count(info.get("scan"))
            if errors > 0:
                function = (info.get("scan") or {}).get("function", "scan")
                await self.notify(
                    f"pool:{name}:scan",
                    f"Pool {name} {function} finished with {errors} errors",
                    severity="error",
                )

    async def _check_pool_capacity(self, threshold: int) -> None:
        """Alert when any pool crosses the configured usage threshold.

        The event key includes a 5 percentage-point bucket so usage climbing
        through 90% -> 95% -> 100% produces a fresh notification at each step
        rather than being suppressed by the cooldown.
        """
        stdout, _, rc = await run_zpool(
            "list", "-p", "-H", "-o", "name,capacity", op="read", check=False
        )
        if rc != 0:
            return
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name, cap = parts[0], parts[1].rstrip("%")
            if not cap.isdigit():
                continue
            pct = int(cap)
            if pct < threshold:
                continue
            bucket = int(pct // 5) * 5
            await self.notify(
                f"pool:{name}:capacity:{bucket}",
                f"Pool {name} usage is at {pct}% (threshold {threshold}%)",
                severity="warning",
            )

    # ── Internals ───────────────────────────────────────────────────────

    def _suppressed(self, event_key: str, cooldown_minutes: int) -> bool:
        last = self._last_sent.get(event_key)
        if last is None:
            return False
        window = max(0, float(cooldown_minutes)) * 60.0
        return (time.monotonic() - last) < window

    async def _deliver(self, message: str, severity: str) -> tuple:
        if severity not in _VALID_SEVERITIES:
            severity = "error"
        settings = get_settings()
        await send_telegram_message(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            message,
        )
        return True, None

    async def _record(
        self,
        event_key: str,
        message: str,
        severity: str,
        delivered: bool,
        error: Optional[str],
    ) -> None:
        """Persist an alert attempt to ``alert_log`` (best-effort)."""
        from ..database import get_db_context
        from ..models.alert import AlertLog

        if severity not in _VALID_SEVERITIES:
            severity = "error"
        try:
            with get_db_context() as db:
                db.add(AlertLog(
                    event_key=event_key,
                    channel="telegram",
                    message=message,
                    severity=severity,
                    delivered=delivered,
                    error=error,
                ))
        except Exception:
            logger.warning("Failed to record alert in alert_log")