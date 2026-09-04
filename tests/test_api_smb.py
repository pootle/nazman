import pytest
from unittest.mock import patch, AsyncMock

from nazman.managers.smb_manager import smb_manager


@pytest.mark.asyncio
async def test_list_shares_empty(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.list_shares = lambda db: []
        response = client.get("/api/smb/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_shares(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.list_shares = lambda db: [{
            "dataset_name": "testpool/data",
            "share_name": "data",
            "share_path": "/testpool/data",
            "read_only": False,
            "guest_ok": True,
            "enabled": True,
        }]
        response = client.get("/api/smb/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["dataset_name"] == "testpool/data"
        assert data[0]["enabled"] is True


@pytest.mark.asyncio
async def test_create_share(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        async def fake_set(db, dataset_name=None, read_only=False, enabled=True):
            return {
                "dataset_name": dataset_name,
                "share_name": "data",
                "share_path": f"/{dataset_name}",
                "read_only": read_only,
                "enabled": enabled,
            }
        mock.set_share = AsyncMock(side_effect=fake_set)
        response = client.post("/api/smb/", json={
            "dataset_name": "testpool/data",
            "read_only": False,
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["dataset_name"] == "testpool/data"
        assert data["enabled"] is True


@pytest.mark.asyncio
async def test_update_share_read_only(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.list_shares = lambda db: [{
            "dataset_name": "testpool/data",
            "share_name": "data",
            "share_path": "/testpool/data",
            "read_only": False,
            "guest_ok": True,
            "enabled": True,
        }]
        async def fake_set(db, dataset_name=None, read_only=False, enabled=True):
            return {
                "dataset_name": dataset_name,
                "share_name": "data",
                "share_path": f"/{dataset_name}",
                "read_only": read_only,
                "enabled": enabled,
            }
        mock.set_share = AsyncMock(side_effect=fake_set)
        response = client.put("/api/smb/testpool/data", json={"read_only": True})
        assert response.status_code == 200, response.text
        assert response.json()["read_only"] is True


@pytest.mark.asyncio
async def test_delete_share(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.delete_share = AsyncMock(return_value=None)
        response = client.delete("/api/smb/testpool/data")
        assert response.status_code == 200
        assert "removed" in response.json()["message"]


@pytest.mark.asyncio
async def test_presence(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.is_server_present = lambda: True
        response = client.get("/api/smb/presence")
        assert response.status_code == 200
        assert response.json() == {"installed": True}


@pytest.mark.asyncio
async def test_install_server(client):
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.install_server = AsyncMock(
            return_value={"installed": True, "message": "Samba installed successfully."})
        response = client.post("/api/smb/install")
        assert response.status_code == 200
        assert response.json()["installed"] is True


@pytest.mark.asyncio
async def test_install_server_error(client):
    from nazman.utils.exceptions import SmbError
    with patch("nazman.api.smb.smb_manager") as mock:
        mock.install_server = AsyncMock(side_effect=SmbError("apt failed"))
        response = client.post("/api/smb/install")
        assert response.status_code == 400
        assert "apt failed" in response.json()["detail"]