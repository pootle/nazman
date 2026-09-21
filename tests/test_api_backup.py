import pytest
from unittest.mock import AsyncMock
from nazman.wiring import get_backup_manager
from tests.conftest import override_manager


@pytest.mark.asyncio
async def test_list_config_bundles_empty(client):
    with override_manager(get_backup_manager) as mock:
        mock.find_config_bundles = lambda db: []
        response = client.get("/api/backup/bundles")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_config_bundles(client):
    with override_manager(get_backup_manager) as mock:
        mock.find_config_bundles = lambda db: [
            {"id": "20260101-000000", "message": "Auto", "volume_root": "/mnt/backup/AAA"},
        ]
        response = client.get("/api/backup/bundles")
        assert response.status_code == 200
        data = response.json()
        assert data[0]["id"] == "20260101-000000"
        assert data[0]["volume_root"] == "/mnt/backup/AAA"


@pytest.mark.asyncio
async def test_restore_backup(client):
    with override_manager(get_backup_manager) as mock:
        mock.restore_configuration = AsyncMock(return_value=True)
        response = client.post("/api/backup/restore", json={"commit_hash": "20260101-000000"})
        assert response.status_code == 200
        assert "restored" in response.json()["message"]


@pytest.mark.asyncio
async def test_restore_backup_failure(client):
    with override_manager(get_backup_manager) as mock:
        mock.restore_configuration = AsyncMock(return_value=False)
        response = client.post("/api/backup/restore", json={"commit_hash": "nonexistent"})
        assert response.status_code == 200
        assert "failed" in response.json()["message"]


def test_backup_page_renders(client):
    resp = client.get("/backup")
    assert resp.status_code == 200
    assert "Backup Disks" in resp.text
    assert "Datasets to Back Up" in resp.text


def test_restore_page_renders(client):
    resp = client.get("/restore")
    assert resp.status_code == 200
    assert "Rebuild This System" in resp.text
