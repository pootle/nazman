import pytest
from unittest.mock import patch, AsyncMock
from nazman.wiring import get_snapshot_manager
from tests.conftest import override_manager


@pytest.mark.asyncio
async def test_list_snapshots_empty(client):
    with override_manager(get_snapshot_manager) as mock:
        mock.list_snapshots = AsyncMock(return_value=[])
        response = client.get("/api/snapshots/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_snapshots(client):
    with override_manager(get_snapshot_manager) as mock:
        mock.list_snapshots = AsyncMock(return_value=[{
            "name": "testpool/data@auto-20240115",
            "dataset_name": "testpool/data",
            "snapshot_name": "auto-20240115",
            "used": "100M",
            "referenced": "1G",
            "creation": "20240115",
        }])
        response = client.get("/api/snapshots/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["snapshot_name"] == "auto-20240115"
        assert data[0]["dataset_name"] == "testpool/data"


@pytest.mark.asyncio
async def test_create_snapshot(client):
    with override_manager(get_snapshot_manager) as mock:
        mock.create_snapshot = AsyncMock(return_value={
            "name": "testpool/data@daily-001",
            "dataset_name": "testpool/data",
            "snapshot_name": "daily-001",
        })
        response = client.post("/api/snapshots/", json={
            "dataset_name": "testpool/data",
            "snapshot_name": "daily-001",
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["snapshot_name"] == "daily-001"


@pytest.mark.asyncio
async def test_destroy_snapshot(client):
    with override_manager(get_snapshot_manager) as mock:
        mock.destroy_snapshot = AsyncMock(return_value=None)
        response = client.delete("/api/snapshots/testpool/data@daily-001")
        assert response.status_code == 200
        assert "destroyed" in response.json()["message"]


@pytest.mark.asyncio
async def test_destroy_snapshot_requires_at_sign(client):
    with patch("nazman.managers.snapshot_manager.run_zfs", new_callable=AsyncMock) as mock_zfs:
        response = client.delete("/api/snapshots/testpool/data")
    assert response.status_code == 400
    assert "must be of the form" in response.json()["detail"]
    mock_zfs.assert_not_called()
