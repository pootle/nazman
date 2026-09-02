import pytest
from unittest.mock import patch, AsyncMock
from nazman.models.pool import Pool
from nazman.utils.commands import run_zfs
from nazman.utils.exceptions import DatasetError


@pytest.mark.asyncio
async def test_list_datasets_empty(client):
    with patch("nazman.api.datasets.zfs_manager") as mock:
        mock.list_datasets = AsyncMock(return_value=[])
        response = client.get("/api/datasets/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_datasets(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    with patch("nazman.api.datasets.zfs_manager") as mock:
        mock.list_datasets = AsyncMock(return_value=[{
            "name": "testpool/data",
            "compression": "zstd",
            "recordsize": "128K",
            "sync_mode": "standard",
            "quota": None,
            "mountpoint": "/testpool/data",
        }])
        response = client.get("/api/datasets/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "testpool/data"


@pytest.mark.asyncio
async def test_get_dataset(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    async def fake_get_props(name):
        return {"compression": "zstd", "recordsize": "128K", "sync_mode": "standard", "quota": None}

    async def fake_exists(name):
        return True

    with patch("nazman.managers.zfs_manager.run_zfs") as mock_run_zfs, \
         patch("nazman.api.datasets.zfs_manager._get_dataset_properties", side_effect=fake_get_props):
        async def fake_run_zfs(*args, **kwargs):
            return ("testpool/data", "", 0)
        mock_run_zfs.side_effect = fake_run_zfs
        response = client.get("/api/datasets/testpool/data")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "testpool/data"


@pytest.mark.asyncio
async def test_get_dataset_not_found(client):
    with patch("nazman.managers.zfs_manager.run_zfs") as mock_run_zfs:
        async def fake_run_zfs(*args, **kwargs):
            return ("", "", 1)
        mock_run_zfs.side_effect = fake_run_zfs
        response = client.get("/api/datasets/does-not-exist")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_dataset(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    with patch("nazman.api.datasets.zfs_manager") as mock:
        async def fake_create(db, name, pool_name, compression, recordsize, sync_mode, quota=None, special_small_blocks=None, atime="partial", canmount="on", readonly="off"):
            full_name = f"{pool_name}/{name}"
            return {
                "name": full_name,
                "compression": compression,
                "recordsize": recordsize,
                "sync_mode": sync_mode,
                "quota": quota,
                "special_small_blocks": special_small_blocks or "0",
                "atime": atime,
                "canmount": canmount,
                "readonly": readonly,
                "mountpoint": f"/{full_name}",
            }

        mock.create_dataset = AsyncMock(side_effect=fake_create)
        response = client.post("/api/datasets/", json={
            "name": "media",
            "pool_name": "testpool",
            "compression": "zstd",
            "recordsize": "1M",
            "sync_mode": "always",
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["name"] == "testpool/media"


@pytest.mark.asyncio
async def test_create_dataset_special_small_blocks(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    with patch("nazman.api.datasets.zfs_manager") as mock:
        async def fake_create(db, name, pool_name, compression, recordsize, sync_mode, quota=None, special_small_blocks=None, atime="partial", canmount="on", readonly="off"):
            full_name = f"{pool_name}/{name}"
            return {
                "name": full_name,
                "compression": compression,
                "recordsize": recordsize,
                "sync_mode": sync_mode,
                "quota": quota,
                "special_small_blocks": special_small_blocks or "0",
                "atime": atime,
                "canmount": canmount,
                "readonly": readonly,
                "mountpoint": f"/{full_name}",
            }

        mock.create_dataset = AsyncMock(side_effect=fake_create)
        response = client.post("/api/datasets/", json={
            "name": "pics",
            "pool_name": "testpool",
            "compression": "zstd",
            "recordsize": "128K",
            "sync_mode": "standard",
            "special_small_blocks": "64K",
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["name"] == "testpool/pics"
        assert data["special_small_blocks"] == "64K"
        # Ensure the value reached the manager call.
        _, kwargs = mock.create_dataset.call_args
        assert kwargs["special_small_blocks"] == "64K"


@pytest.mark.asyncio
async def test_create_dataset_failure_returns_400(client):
    """A DatasetError from create_dataset should surface as 400 with real detail, not 500."""
    with patch("nazman.api.datasets.zfs_manager.create_dataset",
               AsyncMock(side_effect=DatasetError("Failed to create dataset: cannot create 'testpool/media': pool does not exist"))):
        response = client.post("/api/datasets/", json={
            "name": "media",
            "pool_name": "testpool",
            "compression": "zstd",
            "recordsize": "1M",
            "sync_mode": "always",
        })
        assert response.status_code == 400
        assert "pool does not exist" in response.json()["detail"]


@pytest.mark.asyncio
async def test_update_dataset(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    async def fake_get_props(name):
        return {"compression": "lz4", "recordsize": "1M", "sync_mode": "standard", "quota": None}

    with patch("nazman.managers.zfs_manager.run_zfs") as mock_run_zfs, \
         patch("nazman.api.datasets.run_zfs") as mock_api_run_zfs, \
         patch("nazman.api.datasets.zfs_manager._get_dataset_properties", side_effect=fake_get_props):
        async def fake_run_zfs(*args, **kwargs):
            # Existence check (zfs list) must show the dataset name in stdout;
            # other calls (set/get) return cleanly.
            if list(args)[0] == "list":
                return ("testpool/data", "", 0)
            return ("", "", 0)
        mock_run_zfs.side_effect = fake_run_zfs
        mock_api_run_zfs.side_effect = fake_run_zfs
        response = client.put("/api/datasets/testpool/data", json={
            "compression": "lz4",
            "recordsize": "1M",
        })
    assert response.status_code == 200
    data = response.json()
    assert data["compression"] == "lz4"
    assert data["recordsize"] == "1M"


@pytest.mark.asyncio
async def test_update_dataset_special_small_blocks(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    async def fake_get_props(name):
        return {"compression": "zstd", "recordsize": "128K", "sync_mode": "standard", "quota": None, "special_small_blocks": "64K"}

    with patch("nazman.managers.zfs_manager.run_zfs") as mock_run_zfs, \
         patch("nazman.api.datasets.run_zfs") as mock_api_run_zfs, \
         patch("nazman.api.datasets.zfs_manager._get_dataset_properties", side_effect=fake_get_props):
        async def fake_run_zfs(*args, **kwargs):
            if list(args)[0] == "list":
                return ("testpool/data", "", 0)
            return ("", "", 0)
        mock_run_zfs.side_effect = fake_run_zfs
        mock_api_run_zfs.side_effect = fake_run_zfs
        response = client.put("/api/datasets/testpool/data", json={
            "special_small_blocks": "64K",
        })
    assert response.status_code == 200
    set_calls = [c for c in mock_api_run_zfs.call_args_list if c.args and c.args[0] == "set"]
    assert any(c.args[1] == "special_small_blocks=64K" for c in set_calls)


@pytest.mark.asyncio
async def test_update_dataset_special_small_blocks_blank_resets(client, db_session):
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    async def fake_get_props(name):
        return {"compression": "zstd", "recordsize": "128K", "sync_mode": "standard", "quota": None, "special_small_blocks": "0"}

    with patch("nazman.managers.zfs_manager.run_zfs") as mock_run_zfs, \
         patch("nazman.api.datasets.run_zfs") as mock_api_run_zfs, \
         patch("nazman.api.datasets.zfs_manager._get_dataset_properties", side_effect=fake_get_props):
        async def fake_run_zfs(*args, **kwargs):
            if list(args)[0] == "list":
                return ("testpool/data", "", 0)
            return ("", "", 0)
        mock_run_zfs.side_effect = fake_run_zfs
        mock_api_run_zfs.side_effect = fake_run_zfs
        response = client.put("/api/datasets/testpool/data", json={
            "special_small_blocks": "",
        })
    assert response.status_code == 200
    set_calls = [c for c in mock_api_run_zfs.call_args_list if c.args and c.args[0] == "set"]
    assert any(c.args[1] == "special_small_blocks=0" for c in set_calls)


@pytest.mark.asyncio
async def test_update_dataset_not_found(client):
    with patch("nazman.managers.zfs_manager.run_zfs") as mock_run_zfs:
        async def fake_run_zfs(*args, **kwargs):
            return ("", "", 1)
        mock_run_zfs.side_effect = fake_run_zfs
        response = client.put("/api/datasets/does-not-exist", json={
            "compression": "lz4",
        })
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_destroy_dataset(client):
    with patch("nazman.api.datasets.zfs_manager") as mock:
        mock.destroy_dataset = AsyncMock(return_value=None)
        response = client.delete("/api/datasets/testpool/data")
        assert response.status_code == 200
        assert "destroyed" in response.json()["message"]
        args, kwargs = mock.destroy_dataset.call_args
        assert args[1] == "testpool/data"


@pytest.mark.asyncio
async def test_destroy_dataset_returns_400_when_mounted(client):
    with patch("nazman.api.datasets.zfs_manager") as mock:
        mock.destroy_dataset = AsyncMock(
            side_effect=DatasetError(
                'Dataset "testpool/data" is still mounted. Unmount the dataset '
                "and disconnect NFS clients before destroying."
            )
        )
        response = client.delete("/api/datasets/testpool/data")
        assert response.status_code == 400
        assert "mounted" in response.json()["detail"]
        assert "Unmount" in response.json()["detail"]