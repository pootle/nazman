import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock

import httpx

from nazman.config import Settings
from nazman.utils.exceptions import NAZManError
from nazman.utils.telegram import send_telegram_message
from tests.conftest import override_manager


def alert_settings(**overrides):
    base = dict(
        alerts_enabled=True,
        telegram_bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        telegram_chat_id="12345",
        alert_pool_usage_threshold=90,
        alert_cooldown_minutes=60,
        alerts_poll_interval=60,
    )
    base.update(overrides)
    return Settings(**base)


# ── Telegram sender ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_send_success():
    def handler(request):
        assert request.url.path == "/bot123:tok/sendMessage"
        body = json.loads(request.content)
        assert body["chat_id"] == "12345"
        assert body["text"] == "hello"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    assert await send_telegram_message("123:tok", "12345", "hello", transport=transport) is True


@pytest.mark.asyncio
async def test_telegram_send_api_error_surfaces_description():
    def handler(request):
        return httpx.Response(
            400, json={"ok": False, "description": "chat not found"}
        )

    transport = httpx.MockTransport(handler)
    with pytest.raises(NAZManError, match="chat not found"):
        await send_telegram_message("123:tok", "999", "hello", transport=transport)


@pytest.mark.asyncio
async def test_telegram_send_network_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    with pytest.raises(NAZManError, match="Telegram request failed"):
        await send_telegram_message("123:tok", "12345", "hello", transport=transport)


@pytest.mark.asyncio
async def test_telegram_send_missing_config():
    with pytest.raises(NAZManError, match="required"):
        await send_telegram_message("", "12345", "hello")
    with pytest.raises(NAZManError, match="required"):
        await send_telegram_message("123:tok", "", "hello")


# ── AlertManager notify/cooldown ───────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_disabled_does_not_send():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager._record = AsyncMock()
    cfg = alert_settings(alerts_enabled=False)
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        ok = await manager.notify("pool:tank:state", "Pool tank is DEGRADED")
    assert ok is False
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_missing_config_does_not_send():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager._record = AsyncMock()
    cfg = alert_settings(telegram_bot_token="", telegram_chat_id="")
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        ok = await manager.notify("pool:tank:state", "Pool tank is DEGRADED")
    assert ok is False
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_sends_and_records():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager._record = AsyncMock()
    cfg = alert_settings()
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        ok = await manager.notify("pool:tank:state", "Pool tank is DEGRADED")
    assert ok is True
    mock_send.assert_awaited_once_with(
        cfg.telegram_bot_token, cfg.telegram_chat_id, "Pool tank is DEGRADED"
    )
    manager._record.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_cooldown_suppresses_repeat():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager._record = AsyncMock()
    cfg = alert_settings(alert_cooldown_minutes=60)
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        first = await manager.notify("pool:tank:state", "Pool tank is DEGRADED")
        second = await manager.notify("pool:tank:state", "Pool tank is DEGRADED")
    assert first is True
    assert second is False
    assert mock_send.await_count == 1


@pytest.mark.asyncio
async def test_notify_force_bypasses_cooldown():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager._record = AsyncMock()
    cfg = alert_settings(alert_cooldown_minutes=60)
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await manager.notify("k", "one")
        await manager.notify("k", "two", force=True)
    assert mock_send.await_count == 2


@pytest.mark.asyncio
async def test_send_test_requires_config():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    cfg = alert_settings(telegram_bot_token="", telegram_chat_id="")
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg):
        with pytest.raises(NAZManError, match="required"):
            await manager.send_test()


# ── AlertManager pool polling ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_pool_status_alerts_degraded_and_scan_errors():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager.notify = AsyncMock(return_value=True)
    status_json = (
        '{"pools": {"tank": {"state": "DEGRADED", "scan": '
        '{"function": "scrub", "state": "finished", '
        '"errors": {"read": 0, "write": 0, "checksum": 5}}}}}'
    )
    cfg = alert_settings()
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.run_zpool",
               new_callable=AsyncMock) as mock_zpool:
        mock_zpool.return_value = (status_json, "", 0)
        await manager._check_pool_status()

    calls = manager.notify.call_args_list
    event_keys = {c.args[0] for c in calls}
    assert "pool:tank:state" in event_keys
    assert "pool:tank:scan" in event_keys
    scan_call = next(c for c in calls if c.args[0] == "pool:tank:scan")
    assert "5 errors" in scan_call.args[1]


@pytest.mark.asyncio
async def test_check_pool_status_online_is_silent():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager.notify = AsyncMock()
    status_json = (
        '{"pools": {"tank": {"state": "ONLINE", "scan": '
        '{"function": "scrub", "state": "finished", '
        '"errors": {"read": 0, "write": 0, "checksum": 0}}}}}'
    )
    cfg = alert_settings()
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.run_zpool",
               new_callable=AsyncMock) as mock_zpool:
        mock_zpool.return_value = (status_json, "", 0)
        await manager._check_pool_status()
    manager.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_capacity_alerts_over_threshold_only():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager.notify = AsyncMock()
    pool_list = "tank\t95\nbackup\t40\n"
    cfg = alert_settings(alert_pool_usage_threshold=90)
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.run_zpool",
               new_callable=AsyncMock) as mock_zpool:
        mock_zpool.return_value = (pool_list, "", 0)
        await manager._check_pool_capacity(cfg.alert_pool_usage_threshold)

    manager.notify.assert_awaited_once()
    call = manager.notify.call_args
    assert call.args[0] == "pool:tank:capacity:95"
    assert "95%" in call.args[1]
    assert manager.notify.call_args.kwargs["severity"] == "warning"


@pytest.mark.asyncio
async def test_check_noop_when_disabled():
    from nazman.managers.alert_manager import AlertManager

    manager = AlertManager()
    manager.notify = AsyncMock()
    cfg = alert_settings(alerts_enabled=False)
    with patch("nazman.managers.alert_manager.get_settings", return_value=cfg), \
         patch("nazman.managers.alert_manager.run_zpool",
               new_callable=AsyncMock) as mock_zpool:
        await manager._check()
    mock_zpool.assert_not_awaited()


# ── Scheduler failure notification ─────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_notifies_on_task_failure(db_session):
    from types import SimpleNamespace

    from nazman.managers.scheduler import SchedulerManager
    from nazman.models.scheduler import ScheduledTask, TaskType

    alerter = SimpleNamespace(notify=AsyncMock(return_value=True))
    manager = SchedulerManager(alerter=alerter)

    async def failing(task, db):
        raise NAZManError("boom")

    manager._executors["boom"] = failing
    manager._executors[TaskType.SCRUB.value] = failing

    task = ScheduledTask(
        name="doomed", task_type=TaskType.SCRUB.value, target="tank",
        schedule="0 2 * * 0", enabled=True, config={},
    )
    db_session.add(task)
    db_session.commit()

    with patch("nazman.database.get_db_context") as mock_ctx_factory:
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=db_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx_factory.return_value = mock_ctx

        await manager._execute_task(task.id)

    alerter.notify.assert_awaited_once()
    kwargs = alerter.notify.call_args
    assert kwargs.args[0] == "task:doomed"
    assert "doomed" in kwargs.args[1]
    assert "boom" in kwargs.args[1]


@pytest.mark.asyncio
async def test_scheduler_without_alerter_is_silent(db_session):
    from nazman.managers.scheduler import SchedulerManager
    from nazman.models.scheduler import ScheduledTask, TaskHistory, TaskType

    manager = SchedulerManager(alerter=None)

    async def failing(task, db):
        raise NAZManError("boom")

    manager._executors[TaskType.SCRUB.value] = failing
    task = ScheduledTask(
        name="doomed", task_type=TaskType.SCRUB.value, target="tank",
        schedule="0 2 * * 0", enabled=True, config={},
    )
    db_session.add(task)
    db_session.commit()

    with patch("nazman.database.get_db_context") as mock_ctx_factory:
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=db_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx_factory.return_value = mock_ctx

        await manager._execute_task(task.id)

    history = db_session.query(TaskHistory).filter(
        TaskHistory.task_id == task.id
    ).first()
    assert history is not None
    assert history.status == "failed"


# ── API endpoints ──────────────────────────────────────────────────────


def test_api_alerts_config_get(client):
    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["telegram_bot_token"] == ""
    assert data["telegram_chat_id"] == ""
    assert data["pool_usage_threshold"] == 90


def test_api_alerts_config_put(client):
    from nazman.api import alerts as alerts_api

    settings = alerts_api.get_settings()
    recorded = {}

    def fake_set_setting(key, value):
        recorded[key] = value
        setattr(settings, key, value)

    payload = {
        "enabled": True,
        "telegram_bot_token": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "telegram_chat_id": "12345",
        "pool_usage_threshold": 85,
        "cooldown_minutes": 30,
    }
    with patch.object(alerts_api, "set_setting", side_effect=fake_set_setting):
        response = client.put("/api/alerts/config", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["telegram_chat_id"] == "12345"
    assert data["pool_usage_threshold"] == 85
    assert data["cooldown_minutes"] == 30
    assert data["telegram_bot_token"].endswith("AAAA")
    assert recorded["telegram_bot_token"] == payload["telegram_bot_token"]


def test_api_alerts_config_put_masked_token_is_unchanged(client):
    from nazman.api import alerts as alerts_api

    settings = alerts_api.get_settings()
    token = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    setattr(settings, "telegram_bot_token", token)
    mask = alerts_api._mask_token(token)

    recorded = {}
    with patch.object(alerts_api, "set_setting", side_effect=lambda k, v: recorded.setdefault(k, v)):
        response = client.put(
            "/api/alerts/config",
            json={"telegram_bot_token": mask, "telegram_chat_id": "99"},
        )

    assert response.status_code == 200
    assert "telegram_bot_token" not in recorded
    assert recorded["telegram_chat_id"] == "99"


def test_api_alerts_config_put_rejects_bad_inputs(client):
    from nazman.api import alerts as alerts_api

    with patch.object(alerts_api, "set_setting"):
        response = client.put(
            "/api/alerts/config", json={"telegram_bot_token": "not-a-token"}
        )
        assert response.status_code == 400

        response = client.put(
            "/api/alerts/config", json={"telegram_chat_id": "abc"}
        )
        assert response.status_code == 400

        response = client.put(
            "/api/alerts/config", json={"pool_usage_threshold": 200}
        )
        assert response.status_code == 400


def test_api_alerts_config_enable_without_config_rejected(client):
    from nazman.api import alerts as alerts_api

    # Ensure no token/chat configured at all on the settings object.
    settings = alerts_api.get_settings()
    setattr(settings, "telegram_bot_token", "")
    setattr(settings, "telegram_chat_id", "")

    with patch.object(alerts_api, "set_setting", side_effect=lambda k, v: setattr(settings, k, v)):
        response = client.put("/api/alerts/config", json={"enabled": True})
    assert response.status_code == 400


def test_api_alerts_test_success(client):
    from nazman.wiring import get_alert_manager

    with override_manager(get_alert_manager) as mock:
        mock.send_test = AsyncMock(return_value=True)
        response = client.post("/api/alerts/test", json={"message": "hi"})
    assert response.status_code == 200
    mock.send_test.assert_awaited_with("hi")


def test_api_alerts_test_failure_returns_400(client):
    from nazman.wiring import get_alert_manager

    with override_manager(get_alert_manager) as mock:
        mock.send_test = AsyncMock(side_effect=NAZManError("chat not found"))
        response = client.post("/api/alerts/test", json={})
    assert response.status_code == 400
    assert "chat not found" in response.json()["detail"]


def test_api_alerts_history_empty(client):
    response = client.get("/api/alerts/history")
    assert response.status_code == 200
    assert response.json() == []