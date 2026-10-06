import pytest
from pathlib import Path
from unittest.mock import AsyncMock
from nazman.wiring import get_backup_manager, get_backup_group_service
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
    assert "Backup Groups" in resp.text
    assert "backup-groups" in resp.text
    # The per-dataset schedule panel is gone; groups hold the datasets now.
    assert "upsertBackupSchedule" not in resp.text
    # The group modal must fetch its dataset list from a live endpoint.
    assert "getDatasets()" in resp.text
    assert "backup-zfs/datasets" not in resp.text
    # Running sessions get a live progress bar.
    assert "sessionProgressCell" in resp.text
    assert "<th>Progress</th>" in resp.text


def test_backup_page_script_has_no_phantom_functions(client):
    """Every function the disks panel calls on load must actually exist.

    A reference to a renamed-away function made the whole panel report
    "Failed to load backup disks" even though every API call succeeded.
    """
    resp = client.get("/backup")
    source = resp.text.split("<script>", 1)[1].split("</script>", 1)[0]
    for name in ("loadBackupDisks", "renderBackupDisks", "loadBackupGroups",
                 "loadBackupSessions", "renderBackupDisks", "setActiveDisk"):
        assert f"function {name}" in source or f"async function {name}" in source
    assert "renderBackupDatasets" not in source


def test_backup_page_wipe_warning_lists_disk_usage(client):
    """The confirm dialog names what a wipe would destroy (sets/RAID/foreign)."""
    resp = client.get("/backup")
    assert resp.status_code == 200
    assert "info.partitions" in resp.text
    assert "backupDiskRaidInfo" in resp.text
    assert "not managed by NAZMan" in resp.text
    assert "This disk is the " in resp.text
    assert "This disk is a " in resp.text


@pytest.mark.asyncio
async def test_activate_backup_disk_route(client):
    with override_manager(get_backup_group_service) as svc:
        svc.set_active_disk = AsyncMock(return_value={
            "id": 2, "active_disk_id": 41, "disks": [{"id": 40, "is_active": False},
                                                      {"id": 41, "is_active": True}],
        })
        response = client.post("/api/backup-groups/1/sets/2/disks/41/activate")
    assert response.status_code == 200
    assert response.json()["active_disk_id"] == 41
    svc.set_active_disk.assert_awaited_once()


def test_frontend_calls_no_removed_backup_endpoints():
    """api.js is loaded by every page, so a stale binding there breaks silently.

    The schedule and per-dataset backup endpoints went away with backup groups.
    """
    source = (Path(__file__).resolve().parents[1] / "static" / "js" / "api.js").read_text()
    for dead in ("/api/backup-zfs/schedules", "/api/backup-zfs/datasets", "listBackupableDatasets"):
        assert dead not in source, dead
    assert "/api/backup-groups" in source
    assert "getDatasets" in source


def test_restore_page_renders(client):
    resp = client.get("/restore")
    assert resp.status_code == 200
    assert "Rebuild This System" in resp.text
