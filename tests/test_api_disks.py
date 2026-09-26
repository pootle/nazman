import pytest
from unittest.mock import patch, AsyncMock
import json
from contextlib import ExitStack, contextmanager
from nazman.models.disk import Disk
from nazman.models.backup_zfs import BackupDisk
from nazman.utils.devices import refresh_device_map, clear_device_map
from nazman.managers.disk_manager import DiskManager
from nazman.utils.exceptions import DiskError

disk_manager = DiskManager()


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


@contextmanager
def mocked_disk_views(pool_members=None, error_counts=None, backup_disks=None,
                      usage=None):
    """Patch the manager lookups the disk-view service composes per request.

    Yields nothing special; individual mocks are accessible by name via the
    returned ExitStack if a test needs assertions, but most only set returns.
    """
    with ExitStack() as stack:
        stack.enter_context(patch(
            "nazman.managers.zfs_manager.ZfsManager.get_members_and_errors",
            new_callable=AsyncMock,
            return_value=({} if pool_members is None else pool_members,
                          {} if error_counts is None else error_counts)))
        stack.enter_context(patch(
            "nazman.managers.zfs_backup_manager.ZfsBackupManager.list_backup_disks",
            new_callable=AsyncMock,
            return_value=[] if backup_disks is None else backup_disks))
        stack.enter_context(patch(
            "nazman.managers.disk_manager.DiskManager.get_disk_usage",
            new_callable=AsyncMock,
            return_value={} if usage is None else usage))
        yield stack


@pytest.mark.asyncio
async def test_list_disks(client):
    with mocked_disk_views(), \
         patch("nazman.managers.disk_manager.DiskManager.sync_disks_to_database",
               new_callable=AsyncMock, return_value=[]):
        response = client.get("/api/disks/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_disks_with_data(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    _present()

    with mocked_disk_views(usage={disk.id: {"partition_count": 3, "free_percent": 25}}), \
         patch("nazman.managers.disk_manager.DiskManager.sync_disks_to_database",
               new_callable=AsyncMock, return_value=[disk]):
        response = client.get("/api/disks/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["by_id"] == "/dev/disk/by-id/ata-Test_SN123"
        assert data[0]["device_name"] == "sda"
        assert data[0]["serial"] == "SN123"
        assert data[0]["partition_count"] == 3
        assert data[0]["free_percent"] == 25
        assert data[0]["role"] == "unused"


@pytest.mark.asyncio
async def test_enrich_lookups_run_concurrently(db_session):
    """The pool-state, backup, and usage lookups in enrich() are gathered, not
    awaited one after another."""
    import asyncio as _asyncio
    from nazman.managers.zfs_backup_manager import ZfsBackupManager
    from nazman.managers.zfs_manager import ZfsManager
    from nazman.services.disk_view import DiskViewService

    service = DiskViewService(disk_manager, ZfsManager(), ZfsBackupManager())
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    entered = {"members": 0, "backups": 0, "usage": 0}
    release = _asyncio.Event()

    async def fake_members_errors():
        entered["members"] += 1
        await release.wait()
        return {}, {}

    async def fake_list_backup_disks(db):
        entered["backups"] += 1
        await release.wait()
        return []

    async def fake_get_disk_usage(disks):
        entered["usage"] += 1
        await release.wait()
        return {}

    async def run_enrich():
        with patch.object(service.zfs, "get_members_and_errors",
                          side_effect=fake_members_errors), \
             patch.object(service.zfs_backup, "list_backup_disks",
                          side_effect=fake_list_backup_disks), \
             patch.object(service.disk, "get_disk_usage",
                          side_effect=fake_get_disk_usage):
            return await service.enrich(db_session, [disk])

    task = _asyncio.create_task(run_enrich())
    for _ in range(5):
        await _asyncio.sleep(0)
    assert entered == {"members": 1, "backups": 1, "usage": 1}, \
        "enrich lookups are not running concurrently"
    release.set()
    views = await task
    assert views[0]["role"] == "unused"


@pytest.mark.asyncio
async def test_enrich_backup_lookup_failure_falls_back_empty(client, db_session):
    """A failing backup-disk lookup must not break the disks page (-> [] fallback)."""
    from nazman.managers.zfs_backup_manager import ZfsBackupManager
    from nazman.managers.zfs_manager import ZfsManager
    from nazman.services.disk_view import DiskViewService

    service = DiskViewService(disk_manager, ZfsManager(), ZfsBackupManager())
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    with patch.object(service.zfs, "get_members_and_errors",
                      new_callable=AsyncMock, return_value=({}, {})), \
         patch.object(service.zfs_backup, "list_backup_disks",
                      new_callable=AsyncMock,
                      side_effect=RuntimeError("boom")), \
         patch.object(service.disk, "get_disk_usage",
                      new_callable=AsyncMock,
                      return_value={disk.id: {"partition_count": 0, "free_percent": 100}}):
        views = await service.enrich(db_session, [disk])

    assert views[0]["role"] == "unused"
    assert views[0]["backup_state"] is None


@pytest.mark.asyncio
async def test_get_disk(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    with mocked_disk_views():
        response = client.get(f"/api/disks/{disk.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["device_name"] == "sda"
        assert data["by_id"] == "/dev/disk/by-id/ata-Test_SN123"
        assert data["role"] == "unused"


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

    smart_report = {
        "model_name": "Test HDD",
        "health_status": "ok",
        "passed": True,
        "temperature": 35,
        "power_on_hours": 1000,
        "problems": [],
        "attributes": [
            {"id": 1, "name": "Raw_Read_Error_Rate", "value": 100, "worst": 100,
             "thresh": 6, "when_failed": "", "flags": "", "raw": 0},
        ],
        "self_test": [],
        "nvme": None,
    }
    with mocked_disk_views(), \
         patch.object(DiskManager, "live_device_path", return_value="/dev/sda"), \
         patch.object(DiskManager, "get_smart_details",
                      new_callable=AsyncMock, return_value=smart_report):
        response = client.get(f"/api/disks/{disk.id}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["disk"]["device_name"] == "sda"
        assert data["disk"]["zfs_errors"] is None
        assert data["smart"]["health_status"] == "ok"
        assert data["smart"]["temperature"] == 35
        assert data["zfs"]["pool"] is None
        assert data["zfs"]["errors"] is None
        assert data["zfs"]["events"] == []


@pytest.mark.asyncio
async def test_get_disk_health_smart_unavailable(client, db_session):
    """A disk not currently attached reports null SMART instead of a 400,
    so the details modal can still show device and ZFS info."""
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    clear_device_map()

    with mocked_disk_views(), \
         patch.object(DiskManager, "live_device_path",
                      side_effect=DiskError("Disk is not currently present")):
        response = client.get(f"/api/disks/{disk.id}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["smart"] is None
        assert data["disk"]["device_name"] is None


@pytest.mark.asyncio
async def test_get_disk_health_pool_member_surfaces_zfs_errors(client, db_session):
    """Pool-member health includes aggregated ZFS counters + matched events."""
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present()

    counts = {
        f"{disk.by_id}-part1": {"pool": "tank", "read": 3, "write": 5, "cksum": 7, "guid": "1234"},
    }
    events = [
        {"time": "2025-01-02T03:04:05Z", "class": "ereport.fs.zfs.checksum",
         "vdev_guid": "1234", "vdev_path": f"{disk.by_id}-part1",
         "vdev_devid": f"{disk.by_id}-part1"},
    ]
    with mocked_disk_views(
            pool_members={disk.by_id: "tank", f"{disk.by_id}-part1": "tank"},
            error_counts=counts), \
         patch("nazman.managers.zfs_manager.ZfsManager.get_pool_error_counts",
               new_callable=AsyncMock, return_value=counts), \
         patch("nazman.managers.zfs_manager.ZfsManager.get_pool_error_events",
               new_callable=AsyncMock, return_value=events), \
         patch.object(DiskManager, "live_device_path", return_value="/dev/sda"), \
         patch.object(DiskManager, "get_smart_details", new_callable=AsyncMock,
                      return_value={"health_status": "ok", "passed": True,
                                    "temperature": None, "power_on_hours": None,
                                    "problems": [], "attributes": [], "self_test": [],
                                    "model_name": None, "nvme": None}):
        response = client.get(f"/api/disks/{disk.id}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["disk"]["role"] == "pool"
        assert data["disk"]["role_detail"] == "tank"
        assert data["disk"]["zfs_errors"] == {"read": 3, "write": 5, "cksum": 7}
        assert data["zfs"]["pool"] == "tank"
        assert data["zfs"]["errors"] == {"read": 3, "write": 5, "cksum": 7}
        assert len(data["zfs"]["events"]) == 1
        assert data["zfs"]["events"][0]["class"] == "ereport.fs.zfs.checksum"


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

    with patch("nazman.services.disk_view.read_slot_uuids", new_callable=AsyncMock) as m, \
         patch("nazman.services.disk_view.os_reserved_partition_names", new_callable=AsyncMock) as rm:
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
async def test_wipe_disk_in_pool_fails(client, db_session):
    disk = _mk_disk(by_id="/dev/disk/by-id/ata-PoolMember", serial="SN_POOL")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    _present(name="sdb", by_id="/dev/disk/by-id/ata-PoolMember", serial="SN_POOL")

    with patch("nazman.managers.zfs_manager.ZfsManager.get_pool_members",
               new_callable=AsyncMock) as mock_members:
        mock_members.return_value = {disk.by_id: "tank"}
        response = client.post(f"/api/disks/{disk.id}/wipe")

    assert response.status_code == 400
    assert "member of pool" in response.json()["detail"]


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

    with patch("nazman.managers.disk_manager.run_command", new_callable=AsyncMock), \
         patch("nazman.utils.devices.run_command", new_callable=AsyncMock), \
         patch("nazman.managers.disk_manager.write_slot_uuid", new_callable=AsyncMock), \
         patch("nazman.managers.zfs_manager.ZfsManager.get_pool_members",
               new_callable=AsyncMock) as pm:
        pm.return_value = {}
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


@pytest.mark.asyncio
async def test_batch_partition_skips_missing_disk(client):
    response = client.post("/api/disks/batch-partition", json={
        "disk_ids": [999],
        "partitions": [{"size_mb": 1024}],
    })
    assert response.status_code == 200
    data = response.json()
    assert data[0]["success"] is False
    assert "not found" in data[0]["error"]


@pytest.mark.asyncio
async def test_disk_roles(client, db_session):
    """GET /api/disks role detection: pool, backup, system, dead, unused."""
    unused = _mk_disk(by_id="/dev/disk/by-id/ata-Unused", serial="SN_UNUSED")
    pool_disk = _mk_disk(by_id="/dev/disk/by-id/ata-Pool", serial="SN_POOLSON")
    backup_disk = _mk_disk(by_id="/dev/disk/by-id/ata-BackupDisk", serial="SN_BKUP")
    system = _mk_disk(by_id="/dev/disk/by-id/ata-System", serial="SN_SYS", is_os_disk=True)
    dead = _mk_disk(by_id="/dev/disk/by-id/ata-Dead", serial="SN_DEAD", status="dead")
    db_session.add_all([unused, pool_disk, backup_disk, system, dead])
    db_session.commit()
    for d in (unused, pool_disk, backup_disk, system, dead):
        db_session.refresh(d)
    refresh_device_map([
        {"device_name": "sda", "device_path": "/dev/sda", "by_id": unused.by_id, "serial": unused.serial},
        {"device_name": "sdb", "device_path": "/dev/sdb", "by_id": pool_disk.by_id, "serial": pool_disk.serial},
        {"device_name": "sdc", "device_path": "/dev/sdc", "by_id": backup_disk.by_id, "serial": backup_disk.serial},
        {"device_name": "sdd", "device_path": "/dev/sdd", "by_id": system.by_id, "serial": system.serial},
    ])
    db_session.add(BackupDisk(disk_id=backup_disk.id, label="Office drive",
                              mount_point="/mnt/bk", fs_uuid="FS1"))
    db_session.commit()

    with mocked_disk_views(
            pool_members={pool_disk.by_id: "tank", f"{pool_disk.by_id}-part1": "tank"},
            error_counts={f"{pool_disk.by_id}-part1": {"pool": "tank", "read": 1, "write": 0, "cksum": 0, "guid": "1"}},
            backup_disks=[{"disk_id": backup_disk.id, "label": "Office drive", "status": "unmounted"}]), \
         patch("nazman.managers.disk_manager.DiskManager.sync_disks_to_database",
               new_callable=AsyncMock, return_value=[unused, pool_disk, backup_disk, system, dead]):
        response = client.get("/api/disks/")
        assert response.status_code == 200
        by_id = {d["by_id"]: d for d in response.json()}

        assert by_id[unused.by_id]["role"] == "unused"
        assert by_id[pool_disk.by_id]["role"] == "pool"
        assert by_id[pool_disk.by_id]["role_detail"] == "tank"
        assert by_id[pool_disk.by_id]["zfs_errors"] == {"read": 1, "write": 0, "cksum": 0}
        assert by_id[unused.by_id]["zfs_errors"] is None
        assert by_id[backup_disk.by_id]["role"] == "backup"
        assert by_id[backup_disk.by_id]["role_detail"] == "Office drive"
        assert by_id[backup_disk.by_id]["backup_state"] == "unmounted"
        assert by_id[system.by_id]["role"] == "system"
        assert by_id[dead.by_id]["role"] == "dead"


@pytest.mark.asyncio
async def test_get_disk_usage_free_percent():
    """get_disk_usage computes partition count + unpartitioned-space %."""
    disk = _mk_disk(size_bytes=1_000_000_000)
    _present()  # registers /dev/sda in the device map

    slot_info = {
        "/dev/sda": {
            "partitions": [
                {"name": "sda1", "size_bytes": 100_000_000},
                {"name": "sda2", "size_bytes": 500_000_000},
            ]
        }
    }
    with patch("nazman.managers.disk_manager.read_slot_uuids", new_callable=AsyncMock) as m:
        m.return_value = slot_info
        usage = await disk_manager.get_disk_usage([disk])

    assert usage[disk.id]["partition_count"] == 2
    assert usage[disk.id]["free_percent"] == 40


@pytest.mark.asyncio
async def test_get_smart_details_parses_problems():
    """get_smart_details surfaces failing/threshold attributes and problems."""
    smart_json = json.dumps({
        "model_name": "MR7100",
        "device": {"type": "sata"},
        "smart_status": {"passed": False},
        "ata_smart_attributes": {"table": [
            {"id": 1, "name": "Raw_Read_Error_Rate", "value": 100, "worst": 100,
             "thresh": 16, "when_failed": "", "flags": {"string": "POSR--"},
             "raw": {"value": 0}},
            {"id": 5, "name": "Reallocated_Sector_Ct", "value": 10, "worst": 10,
             "thresh": 36, "when_failed": "NOW", "flags": {"string": "POSR--"},
             "raw": {"value": 120}},
            {"id": 9, "name": "Power_On_Hours", "value": 9876, "worst": 9876,
             "thresh": 0, "when_failed": "", "flags": {"string": "-O---"},
             "raw": {"value": 12345}},
            {"id": 194, "name": "Temperature_Celsius", "value": 40, "worst": 40,
             "thresh": 0, "when_failed": "", "flags": {"string": "-O---"},
             "raw": {"value": 40}},
            {"id": 197, "name": "Current_Pending_Sector", "value": 100, "worst": 100,
             "thresh": 0, "when_failed": "", "flags": {"string": "----"},
             "raw": {"value": 3}},
        ]},
        "ata_smart_self_test_log": {"table": [
            {"type": "Short offline", "status": "Completed without error",
             "remaining": "100%", "lifetime_hours": 12300},
            {"type": "Short offline", "status": "Completed: read failure",
             "remaining": "10%", "lifetime_hours": 12400},
        ]},
    })

    with patch("nazman.managers.disk_manager.run_command", new_callable=AsyncMock,
               return_value=(smart_json, "", 0)) as mock_run:
        details = await disk_manager.get_smart_details("/dev/sda")

    assert details["health_status"] == "failing"
    assert details["passed"] is False
    assert details["temperature"] == 40
    assert details["power_on_hours"] == 9876
    assert len(details["attributes"]) == 5
    assert details["attributes"][1]["when_failed"] == "NOW"
    assert details["problems"][0] == "SMART overall-health self-assessment FAILED"
    assert any("Reallocated_Sector_Ct below threshold" in p for p in details["problems"])
    assert any("Current_Pending_Sector: 3" in p for p in details["problems"])
    assert [t["failed"] for t in details["self_test"]] == [False, True]
    mock_run.assert_called_once_with(
        ["smartctl", "-a", "-j", "/dev/sda"],
        timeout=30, check=False, op="read", category="smartctl",
    )


@pytest.mark.asyncio
async def test_get_smart_details_unavailable():
    """Unusable SMART output degrades to a stub with a problem message."""
    with patch("nazman.managers.disk_manager.run_command", new_callable=AsyncMock,
               return_value=("", "smartctl: unable to open", 1)):
        details = await disk_manager.get_smart_details("/dev/sda")

    assert details["health_status"] == "unknown"
    assert details["attributes"] == []
    assert details["problems"] == ["SMART data unavailable"]


@pytest.mark.asyncio
async def test_recreate_partitions_endpoint(client, db_session):
    disk = _mk_disk()
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)

    with patch.object(DiskManager, "recreate_partition_layout", new_callable=AsyncMock,
                      return_value={"disk_id": disk.id, "success": True}) as m:
        resp = client.post(f"/api/disks/{disk.id}/recreate-partitions", json={
            "partitions": [
                {"size_mb": 100, "slot_uuid": "slot-1"},
                {"size_mb": None, "slot_uuid": "slot-2"},
            ]
        })

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert m.await_args.args[1] == disk.id
    assert m.await_args.args[2] == [
        {"size_mb": 100, "slot_uuid": "slot-1"},
        {"size_mb": None, "slot_uuid": "slot-2"},
    ]
