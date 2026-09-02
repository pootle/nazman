import pytest
from unittest.mock import patch, AsyncMock
from nazman.models.disk import Disk
from nazman.managers.disk_manager import refresh_device_map, clear_device_map


def _mk_disk(name="sda", by_id="/dev/disk/by-id/ata-Test_SN123", serial="SN123", **kw):
    """Create a Disk row using the canonical by_id identity (no ephemeral names)."""
    return Disk(
        by_id=by_id,
        model=kw.pop("model", "Test HDD"),
        serial=serial,
        size_bytes=kw.pop("size_bytes", 1_000_000_000_000),
        disk_type=kw.pop("disk_type", "hdd"),
        health_status=kw.pop("health_status", "ok"),
        **kw,
    )


def _present(name="sda", by_id="/dev/disk/by-id/ata-Test_SN123", serial="SN123"):
    """Register the ephemeral kernel name in the in-memory map."""
    refresh_device_map([{
        "device_name": name,
        "device_path": f"/dev/{name}",
        "by_id": by_id,
        "serial": serial,
    }])


@pytest.mark.asyncio
async def test_list_disks(client):
    with patch("nazman.api.disks.disk_manager") as mock:
        mock.sync_disks_to_database = AsyncMock(return_value=[])
        response = client.get("/api/disks/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_disks_with_data(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    _present()

    with patch("nazman.api.disks.disk_manager") as mock:
        mock.sync_disks_to_database = AsyncMock(return_value=[disk])
        response = client.get("/api/disks/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["by_id"] == "/dev/disk/by-id/ata-Test_SN123"
        assert data[0]["device_name"] == "sda"
        assert data[0]["serial"] == "SN123"


@pytest.mark.asyncio
async def test_get_disk(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    response = client.get(f"/api/disks/{disk.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["device_name"] == "sda"
    assert data["by_id"] == "/dev/disk/by-id/ata-Test_SN123"


@pytest.mark.asyncio
async def test_get_disk_not_found(client):
    response = client.get("/api/disks/999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_disk_health(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    with patch("nazman.api.disks.disk_manager") as mock:
        mock.get_disk_health = AsyncMock(return_value={
            "temperature": 35,
            "power_on_hours": 1000,
            "health_status": "ok",
        })
        response = client.get(f"/api/disks/{disk.id}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["health_status"] == "ok"
        assert data["temperature"] == 35


@pytest.mark.asyncio
async def test_get_disk_health_not_present(client, db_session):
    """A disk not currently attached returns a clear error (no SMART read)."""
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    clear_device_map()

    with patch("nazman.api.disks.disk_manager") as mock:
        response = client.get(f"/api/disks/{disk.id}/health")
        assert response.status_code == 400
        assert "not currently present" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_disk_partitions_not_found(client):
    response = client.get("/api/disks/999/partitions")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_disk_partitions_nvme(client, db_session):
    disk = _mk_disk(by_id="/dev/disk/by-id/nvme-INTEL_TEST", serial="SNNVME",
                    disk_type="nvme")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present(name="nvme0n1", by_id="/dev/disk/by-id/nvme-INTEL_TEST", serial="SNNVME")

    with patch("nazman.api.disks.read_slot_uuids", new_callable=AsyncMock) as m, \
         patch("nazman.api.disks.get_os_reserved_partition_names", new_callable=AsyncMock) as rm:
        rm.return_value = {"nvme0n1p1"}
        m.return_value = {
            "/dev/nvme0n1": {
                "partitions": [
                    {"name": "nvme0n1p1", "partlabel": "nazman:uuid-1",
                     "slot_uuid": "uuid-1", "size_bytes": 1_000_000_000},
                    {"name": "nvme0n1p2", "partlabel": "nazman:uuid-2",
                     "slot_uuid": "uuid-2", "size_bytes": 2_000_000_000_000},
                ],
            }
        }
        response = client.get(f"/api/disks/{disk.id}/partitions")
        assert response.status_code == 200
        data = response.json()
        assert len(data["partitions"]) == 2
        assert data["partitions"][0]["number"] == 1
        assert data["partitions"][0]["reserved"] is True
        assert data["partitions"][1]["number"] == 2
        assert data["partitions"][1]["reserved"] is False
        assert data["partitions"][1]["device_path"] == "/dev/disk/by-id/nvme-INTEL_TEST-part2"


@pytest.mark.asyncio
async def test_wipe_disk_not_found(client):
    response = client.post("/api/disks/999/wipe")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_wipe_os_disk_fails(client, db_session):
    disk = _mk_disk(by_id="/dev/disk/by-id/ata-OS", serial="SN_OS",
                    model="Test SSD", disk_type="ssd", size_bytes=128_000_000_000,
                    is_os_disk=True)
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present(name="sda", by_id="/dev/disk/by-id/ata-OS", serial="SN_OS")

    response = client.post(f"/api/disks/{disk.id}/wipe")
    assert response.status_code == 400
    assert "OS disk" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_disk(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    response = client.patch(f"/api/disks/{disk.id}", json={"status": "dead"})
    assert response.status_code == 200
    assert response.json()["status"] == "dead"


@pytest.mark.asyncio
async def test_resurrect_disk(client, db_session):
    disk = _mk_disk(status="dead")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    response = client.post(f"/api/disks/{disk.id}/resurrect")
    assert response.status_code == 200
    assert "resurrected" in response.json()["message"]


@pytest.mark.asyncio
async def test_resurrect_non_dead_disk_fails(client, db_session):
    disk = _mk_disk(status="active")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    response = client.post(f"/api/disks/{disk.id}/resurrect")
    assert response.status_code == 400
    assert "not dead" in response.json()["detail"]


@pytest.mark.asyncio
async def test_batch_partition(client, db_session):
    disks = []
    for idx, (name, byid) in enumerate([("sda", "/dev/disk/by-id/ata-A"),
                                        ("sdb", "/dev/disk/by-id/ata-B")]):
        d = _mk_disk(by_id=byid, serial=f"SN_{name}", model="Test",
                     disk_type="ssd", size_bytes=1_000_000_000_000)
        db_session.add(d)
        disks.append(d)
    db_session.commit()
    for d in disks:
        db_session.refresh(d)
    refresh_device_map([
        {"device_name": "sda", "device_path": "/dev/sda", "by_id": "/dev/disk/by-id/ata-A", "serial": "SN_sda"},
        {"device_name": "sdb", "device_path": "/dev/sdb", "by_id": "/dev/disk/by-id/ata-B", "serial": "SN_sdb"},
    ])

    with patch("nazman.api.disks.run_command", new_callable=AsyncMock), \
         patch("nazman.api.disks.write_slot_uuid", new_callable=AsyncMock):
        response = client.post("/api/disks/batch-partition", json={
            "disk_ids": [disks[0].id, disks[1].id],
            "partitions": [{"size_mb": 1024}, {"size_mb": None}],
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data) == 2
        assert all(r["success"] for r in data)


@pytest.mark.asyncio
async def test_drop_removed_disk(client, db_session):
    disk = _mk_disk(status="removed")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    clear_device_map()

    response = client.delete(f"/api/disks/{disk.id}")
    assert response.status_code == 200
    assert "Dropped" in response.json()["message"]
    assert db_session.query(Disk).filter(Disk.id == disk.id).count() == 0


@pytest.mark.asyncio
async def test_drop_present_disk_fails(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    response = client.delete(f"/api/disks/{disk.id}")
    assert response.status_code == 400
    assert "currently present" in response.json()["detail"]


@pytest.mark.asyncio
async def test_drop_nonexistent_disk(client):
    response = client.delete("/api/disks/999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_batch_partition_skips_os_disk(client, db_session):
    d = _mk_disk(by_id="/dev/disk/by-id/ata-OS", serial="SN_OS", model="Test",
                 disk_type="ssd", size_bytes=128_000_000_000, is_os_disk=True)
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    _present(name="sda", by_id="/dev/disk/by-id/ata-OS", serial="SN_OS")

    response = client.post("/api/disks/batch-partition", json={
        "disk_ids": [d.id],
        "partitions": [{"size_mb": 1024}],
    })
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["success"] is False
    assert "OS disk" in data[0]["error"]
