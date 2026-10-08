from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nazman.main import app
from nazman.models.scheduler import ScheduledTask, TaskHistory
from nazman.services.task_status import TaskStatusService
from nazman.wiring import get_task_status_service


@pytest.mark.asyncio
async def test_active_tasks_composes_all_sources(db_session):
    backup_groups = MagicMock()
    backup_groups.list_sessions = AsyncMock(return_value=[
        {"id": 1, "status": "running", "group_name": "nightly",
         "progress_pct": 40, "started_at": None, "current_dataset": "tank/a"},
        {"id": 2, "status": "success"},
    ])
    system_restore = MagicMock()
    system_restore.active_jobs = MagicMock(return_value=[{
        "kind": "restore", "id": "AAA", "label": "Restore set AAA",
        "progress_pct": 10, "started_at": None, "detail": "1/2", "link": "/restore",
    }])
    task = ScheduledTask(name="Weekly scrub", task_type="scrub",
                         target="tank", schedule="0 2 * * 0")
    db_session.add(task)
    db_session.commit()
    db_session.add(TaskHistory(task_id=task.id, status="running",
                               started_at=datetime.now(timezone.utc)))
    db_session.commit()

    svc = TaskStatusService(backup_groups=backup_groups,
                            system_restore=system_restore, scheduler=MagicMock())
    tasks = await svc.active_tasks(db_session)

    assert sorted(t["kind"] for t in tasks) == ["backup", "restore", "scheduled"]
    backup = next(t for t in tasks if t["kind"] == "backup")
    assert backup["label"] == "nightly"
    assert backup["progress_pct"] == 40
    assert backup["link"] == "/backup"
    scheduled = next(t for t in tasks if t["kind"] == "scheduled")
    assert scheduled["label"] == "Scrub"
    assert scheduled["link"] == "/settings"


@pytest.mark.asyncio
async def test_active_tasks_empty_when_idle(db_session):
    backup_groups = MagicMock()
    backup_groups.list_sessions = AsyncMock(return_value=[{"id": 9, "status": "success"}])
    system_restore = MagicMock()
    system_restore.active_jobs = MagicMock(return_value=[])
    svc = TaskStatusService(backup_groups=backup_groups,
                            system_restore=system_restore, scheduler=MagicMock())
    assert await svc.active_tasks(db_session) == []


def test_active_tasks_endpoint(client):
    fake = MagicMock()
    fake.active_tasks = AsyncMock(return_value=[{
        "kind": "backup", "id": 1, "label": "nightly", "progress_pct": 50,
        "started_at": None, "detail": None, "link": "/backup",
    }])
    app.dependency_overrides[get_task_status_service] = lambda: fake
    try:
        resp = client.get("/api/tasks/active")
    finally:
        app.dependency_overrides.pop(get_task_status_service, None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["tasks"][0]["kind"] == "backup"