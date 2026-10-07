import pytest
from unittest.mock import patch, AsyncMock
from nazman.models.pool import Pool
from tests.conftest import override_manager
from nazman.wiring import get_zfs_manager, get_destruction_service



@pytest.mark.asyncio
async def test_list_pools_empty(client):
    with override_manager(get_zfs_manager) as mock:
        mock.list_pools = AsyncMock(return_value=[])
        response = client.get("/api/pools/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_pools(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()

    with override_manager(get_zfs_manager) as mock:
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
    with override_manager(get_zfs_manager) as mock:
        mock.get_pool_status = AsyncMock(return_value={
            "name": "testpool",
            "status": "ONLINE",
            "topology": "stripe",
            "vdevs": [],
            "scan": {},
            "ashift": 12,
            "sector_size_bytes": 4096,
        })
        response = client.get("/api/pools/testpool")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "testpool"
        assert data["ashift"] == 12
        assert data["sector_size_bytes"] == 4096


@pytest.mark.asyncio
async def test_create_pool(client, db_session):
    with override_manager(get_zfs_manager) as mock:
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
async def test_create_pool_invalid_topology(client):
    with override_manager(get_zfs_manager) as mock:
        mock.create_pool = AsyncMock()
        response = client.post("/api/pools/", json={
            "name": "newpool",
            "vdevs": [
                {"role": "data", "topology": "invalid-topology", "devices": [
                    {"disk_id": 1},
                ]},
            ],
            "ashift": 12,
        })
        assert response.status_code == 422
        mock.create_pool.assert_not_called()


@pytest.mark.asyncio
async def test_create_pool_ashift_out_of_bounds(client):
    with override_manager(get_zfs_manager) as mock:
        mock.create_pool = AsyncMock()
        response = client.post("/api/pools/", json={
            "name": "newpool",
            "vdevs": [
                {"role": "data", "topology": "mirror", "devices": [
                    {"disk_id": 1},
                    {"disk_id": 2},
                ]},
            ],
            "ashift": 20,
        })
        assert response.status_code == 422
        mock.create_pool.assert_not_called()


@pytest.mark.asyncio
async def test_scrub_pool(client):
    with override_manager(get_zfs_manager) as mock:
        mock.scrub_pool = AsyncMock(return_value=None)
        response = client.post("/api/pools/testpool/scrub")
        assert response.status_code == 200
        assert "Scrub started" in response.json()["message"]


@pytest.mark.asyncio
async def test_export_pool(client):
    with override_manager(get_zfs_manager) as mock:
        mock.export_pool = AsyncMock(return_value=None)
        response = client.post("/api/pools/testpool/export")
        assert response.status_code == 200
        assert "exported" in response.json()["message"]


@pytest.mark.asyncio
async def test_import_pool(client):
    with override_manager(get_zfs_manager) as mock:
        mock.import_pool = AsyncMock(return_value=None)
        response = client.post("/api/pools/testpool/import")
        assert response.status_code == 200
        assert "imported" in response.json()["message"]


@pytest.mark.asyncio
async def test_destroy_pool(client):
    with override_manager(get_destruction_service) as mock:
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

    with override_manager(get_zfs_manager) as mock:
        mock.remove_device = AsyncMock(return_value={"name": "testpool", "removed": "/dev/sdc"})
        response = client.delete("/api/pools/testpool/devices/dev/sdc")
        assert response.status_code == 200
