import pytest
from unittest.mock import patch, AsyncMock
from nazman.models.pool import Pool


@pytest.mark.asyncio
async def test_list_pools_empty(client):
    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.list_pools = AsyncMock(return_value=[])
        response = client.get("/api/pools/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_pools(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()

    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.list_pools = AsyncMock(return_value=[{
            "id": pool.id,
            "name": "testpool",
            "status": "ONLINE",
            "topology": "stripe",
            "size_bytes": 1000000,
        }])
        response = client.get("/api/pools/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "testpool"


@pytest.mark.asyncio
async def test_get_pool_status(client):
    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.get_pool_status = AsyncMock(return_value={
            "name": "testpool",
            "status": "ONLINE",
            "topology": "stripe",
            "vdevs": [],
            "scan": {},
        })
        response = client.get("/api/pools/testpool")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "testpool"


@pytest.mark.asyncio
async def test_create_pool(client, db_session):
    with patch("nazman.api.pools.zfs_manager") as mock:
        async def fake_create_pool(db, name, vdevs, ashift=12):
            pool = Pool(name=name)
            db_session.add(pool)
            db_session.commit()
            db_session.refresh(pool)
            return {
                "id": pool.id,
                "name": pool.name,
                "created_at": pool.created_at.isoformat() if pool.created_at else None,
            }

        mock.create_pool = AsyncMock(side_effect=fake_create_pool)
        response = client.post("/api/pools/", json={
            "name": "newpool",
            "vdevs": [
                {"role": "data", "topology": "mirror", "devices": [
                    {"disk_id": 1, "slot_uuid": None},
                    {"disk_id": 2, "slot_uuid": None},
                ]},
            ],
            "ashift": 12,
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["name"] == "newpool"


@pytest.mark.asyncio
async def test_scrub_pool(client):
    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.scrub_pool = AsyncMock(return_value=None)
        response = client.post("/api/pools/testpool/scrub")
        assert response.status_code == 200
        assert "Scrub started" in response.json()["message"]


@pytest.mark.asyncio
async def test_export_pool(client):
    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.export_pool = AsyncMock(return_value=None)
        response = client.post("/api/pools/testpool/export")
        assert response.status_code == 200
        assert "exported" in response.json()["message"]


@pytest.mark.asyncio
async def test_import_pool(client):
    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.import_pool = AsyncMock(return_value=None)
        response = client.post("/api/pools/testpool/import")
        assert response.status_code == 200
        assert "imported" in response.json()["message"]


@pytest.mark.asyncio
async def test_destroy_pool(client):
    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.destroy_pool = AsyncMock(return_value=None)
        response = client.delete("/api/pools/testpool")
        assert response.status_code == 200
        assert "destroyed" in response.json()["message"]


@pytest.mark.asyncio
async def test_remove_device(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    with patch("nazman.api.pools.zfs_manager") as mock:
        mock.remove_device = AsyncMock(return_value={"name": "testpool", "removed": "/dev/sdc"})
        response = client.delete("/api/pools/testpool/devices/dev/sdc")
        assert response.status_code == 200
