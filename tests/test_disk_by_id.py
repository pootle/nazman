import pytest
from unittest.mock import patch, AsyncMock

from nazman.models.disk import Disk
from nazman.managers.disk_manager import DiskManager, partition_by_id
from nazman.managers.disk_manager import resolve_slot_to_device
from nazman.managers.disk_manager import clear_device_map, refresh_device_map


@pytest.mark.parametrize("disk_by_id,num,expected", [
    (None, 1, None),
    ("/dev/disk/by-id/ata-ST3000_Z300T7LM", 1, "/dev/disk/by-id/ata-ST3000_Z300T7LM-part1"),
    ("/dev/disk/by-id/nvme-SSD_A1", 2, "/dev/disk/by-id/nvme-SSD_A1-part2"),
])
def test_partition_by_id(disk_by_id, num, expected):
    assert partition_by_id(disk_by_id, num) == expected


def test_resolve_slot_to_device_nvme():
    """NVMe partition names (nvme0n1p3) must resolve to their by-id path,
    not the earlier sdX-only partition-number extraction that broke on them."""
    parts = [
        {"name": "nvme0n1p1", "slot_uuid": "abc-1", "size_bytes": 1000},
        {"name": "nvme0n1p3", "slot_uuid": "ghi-3", "size_bytes": 2000},
    ]
    by_id = "/dev/disk/by-id/nvme-INTEL_A1"
    assert resolve_slot_to_device(by_id, "ghi-3", parts) == \
        "/dev/disk/by-id/nvme-INTEL_A1-part3"
    assert resolve_slot_to_device(by_id, "missing", parts) is None


def _discovered(name="sdb", by_id="/dev/disk/by-id/ata-NEW", serial="SN123", **kw):
    return {
        "device_name": name,
        "device_path": f"/dev/{name}",
        "by_id": by_id,
        "model": kw.pop("model", "Model"),
        "serial": serial,
        "size_bytes": kw.pop("size_bytes", 1000),
        "disk_type": kw.pop("disk_type", "hdd"),
        "rotation_speed": 0,
        "is_os_disk": False,
        **kw,
    }


@pytest.mark.asyncio
async def test_sync_disk_reconnects_by_id(db_session):
    """A disk that reappears (same by_id) is updated in place, not duplicated.

    In the new model there is no persisted device_name; the same physical disk
    keyed by by_id is reconciled to a single row.  The in-memory device map
    records the (now ephemeral) kernel name."""
    dm = DiskManager()
    by_id = "/dev/disk/by-id/ata-SSN_NEW"
    discovered = [_discovered(name="sdb", by_id=by_id, serial="SN123")]

    db_session.add(Disk(by_id=by_id, serial="SN123", model="Model",
                        size_bytes=1000, disk_type="hdd"))
    db_session.commit()

    with patch.object(dm, "discover_disks", new_callable=AsyncMock,
                      return_value=discovered), \
         patch.object(dm, "get_disk_health", new_callable=AsyncMock,
                      return_value={"temperature": None, "power_on_hours": None,
                                   "health_status": "ok"}):
        disks = await dm.sync_disks_to_database(db_session)

    assert len(disks) == 1
    row = db_session.query(Disk).one()
    assert row.by_id == by_id
    assert row.serial == "SN123"
    assert row.status == "active"
    # Kernel name is ephemeral, recorded in the in-memory map (not the DB).
    from nazman.managers.disk_manager import get_device_name
    assert get_device_name(row) == "sdb"
    clear_device_map()


@pytest.mark.asyncio
async def test_sync_swap_inserts_new_row_retains_old(db_session):
    """A swapped-in physical disk (new by_id/serial) lands as a NEW row.

    The kernel name is ephemeral: the replacement appearing at the same /dev/sdX
    does NOT rebadge the old row.  The stale/replaced disk row is retained and
    marked 'removed' so knowledge of it is preserved."""
    dm = DiskManager()
    old_by_id = "/dev/disk/by-id/ata-OLD_DISK"
    new_by_id = "/dev/disk/by-id/ata-Samsung_SSD_750_EVO_500GB_S36SNWAH659042L"

    # The dodgy disk that was previously tracked.
    db_session.add(Disk(by_id=old_by_id, serial="OLD_SERIAL",
                        model="Old Disk", size_bytes=1000, disk_type="hdd"))
    db_session.commit()
    old_id = db_session.query(Disk).filter(Disk.by_id == old_by_id).one().id

    # Now only the replacement is present in the system.
    discovered = [_discovered(by_id=new_by_id, serial="S36SNWAH659042L",
                              model="Samsung SSD 750", size_bytes=500148941619,
                              disk_type="ssd")]

    with patch.object(dm, "discover_disks", new_callable=AsyncMock,
                      return_value=discovered), \
         patch.object(dm, "get_disk_health", new_callable=AsyncMock,
                      return_value={"temperature": None, "power_on_hours": None,
                                   "health_status": "ok"}):
        disks = await dm.sync_disks_to_database(db_session)

    # Two rows: the old (removed) and the new (active). No UNIQUE crash.
    rows = db_session.query(Disk).order_by(Disk.id).all()
    assert len(rows) == 2
    old_row = db_session.get(Disk, old_id)
    assert old_row.by_id == old_by_id
    assert old_row.status == "removed"
    new_rows = [r for r in rows if r.by_id == new_by_id]
    assert len(new_rows) == 1
    assert new_rows[0].status == "active"
    assert new_rows[0].serial == "S36SNWAH659042L"
    clear_device_map()


@pytest.mark.asyncio
async def test_sync_marks_absent_disks_removed(db_session):
    """Disks that disappear from discovery are retained and marked 'removed'."""
    dm = DiskManager()
    present = "/dev/disk/by-id/ata-PRESENT"
    gone = "/dev/disk/by-id/ata-GONE"
    db_session.add(Disk(by_id=present, serial="P1", model="Present",
                        size_bytes=1000, disk_type="hdd", status="active"))
    db_session.add(Disk(by_id=gone, serial="G1", model="Gone",
                        size_bytes=1000, disk_type="hdd", status="active"))
    db_session.commit()

    discovered = [_discovered(name="sda", by_id=present, serial="P1")]

    with patch.object(dm, "discover_disks", new_callable=AsyncMock,
                      return_value=discovered), \
         patch.object(dm, "get_disk_health", new_callable=AsyncMock,
                      return_value={"temperature": None, "power_on_hours": None,
                                   "health_status": "ok"}):
        await dm.sync_disks_to_database(db_session)

    gone_row = db_session.query(Disk).filter(Disk.by_id == gone).one()
    present_row = db_session.query(Disk).filter(Disk.by_id == present).one()
    assert gone_row.status == "removed"
    assert present_row.status == "active"
    clear_device_map()


@pytest.mark.asyncio
async def test_sync_reactivates_removed_disk_when_seen_again(db_session):
    """A removed disk that comes back is reactivated to active."""
    dm = DiskManager()
    by_id = "/dev/disk/by-id/ata-BACK"
    db_session.add(Disk(by_id=by_id, serial="B1", model="Back",
                        size_bytes=1000, disk_type="hdd", status="removed"))
    db_session.commit()

    discovered = [_discovered(name="sda", by_id=by_id, serial="B1")]

    with patch.object(dm, "discover_disks", new_callable=AsyncMock,
                      return_value=discovered), \
         patch.object(dm, "get_disk_health", new_callable=AsyncMock,
                      return_value={"temperature": None, "power_on_hours": None,
                                   "health_status": "ok"}):
        disks = await dm.sync_disks_to_database(db_session)

    assert len(disks) == 1
    assert disks[0].status == "active"
    clear_device_map()


@pytest.mark.asyncio
async def test_discover_skips_mmc_boot_subdevices():
    """mmcblk*boot*/rpmb must not be treated as independent disks (they share
    the parent eMMC's by-id/serial and caused UNIQUE device_name collisions)."""
    dm = DiskManager()

    lsblk_full = {
        "blockdevices": [
            {"name": "mmcblk0", "size": "58.3G", "type": "disk", "model": "",
             "serial": "0xc8972379", "rota": 1, "tran": "mmc"},
            {"name": "mmcblk0boot0", "size": "4M", "type": "disk", "model": "",
             "serial": "0xc8972379", "rota": 1, "tran": "mmc"},
            {"name": "mmcblk0boot1", "size": "4M", "type": "disk", "model": "",
             "serial": "0xc8972379", "rota": 1, "tran": "mmc"},
            {"name": "sda", "size": "223.6G", "type": "disk", "model": "Crucial",
             "serial": "14210C269951", "rota": 0, "tran": "sata"},
        ]
    }
    import json as _json

    async def fake_run_command(cmd, timeout=300, check=True, capture_output=True, input=None, **kwargs):
        if cmd[0] == "findmnt":
            return ("/dev/mapper/ubuntu--vg-ubuntu--lv", "", 0)
        if cmd[0] == "lsblk" and "PKNAME" in cmd:
            return (_json.dumps({"blockdevices": [
                {"name": "ubuntu--vg-ubuntu--lv", "type": "lvm", "pkname": ""}
            ]}), "", 0)
        if cmd[0] == "lsblk":
            return (_json.dumps(lsblk_full), "", 0)
        return ("", "", 0)

    with patch("nazman.managers.disk_manager.run_command", side_effect=fake_run_command):
        disks = await dm.discover_disks()

    names = {d["device_name"] for d in disks}
    assert "mmcblk0boot0" not in names
    assert "mmcblk0boot1" not in names
    # The parent eMMC and normal SATA disks are retained.
    assert "mmcblk0" in names
    assert "sda" in names


@pytest.mark.asyncio
async def test_discover_marks_both_raid_mirrors_as_os_disk():
    """A root md RAID spanning two physical disks must mark BOTH disks as OS
    disks (the earlier single-parent walk only caught one)."""
    import json as _json
    dm = DiskManager()

    lsblk_pkname = {
        "blockdevices": [
            {"name": "nvme0n1", "type": "disk", "pkname": None, "children": [
                {"name": "nvme0n1p2", "type": "part", "pkname": "nvme0n1", "children": [
                    {"name": "md0", "type": "raid1", "pkname": "nvme0n1p2", "children": [
                        {"name": "md0p1", "type": "part", "pkname": "md0"}
                    ]}
                ]},
                {"name": "nvme0n1p3", "type": "part", "pkname": "nvme0n1"},
            ]},
            {"name": "nvme1n1", "type": "disk", "pkname": None, "children": [
                {"name": "nvme1n1p3", "type": "part", "pkname": "nvme1n1", "children": [
                    {"name": "md0", "type": "raid1", "pkname": "nvme1n1p3", "children": [
                        {"name": "md0p1", "type": "part", "pkname": "md0"}
                    ]}
                ]},
                {"name": "nvme1n1p4", "type": "part", "pkname": "nvme1n1"},
            ]},
        ]
    }
    lsblk_full = {
        "blockdevices": [
            {"name": "nvme0n1", "size": "476.9G", "type": "disk", "model": "INTEL",
             "serial": "A1", "rota": 0, "tran": "nvme"},
            {"name": "nvme1n1", "size": "465.8G", "type": "disk", "model": "WDC",
             "serial": "B2", "rota": 0, "tran": "nvme"},
        ]
    }

    async def fake_run_command(cmd, timeout=300, check=True, capture_output=True, input=None, **kwargs):
        if cmd[0] == "findmnt":
            return ("/dev/md0p1", "", 0)
        if cmd[0] == "lsblk" and "PKNAME" in " ".join(cmd):
            return (_json.dumps(lsblk_pkname), "", 0)
        if cmd[0] == "lsblk":
            return (_json.dumps(lsblk_full), "", 0)
        return ("", "", 0)

    with patch("nazman.managers.disk_manager.run_command", side_effect=fake_run_command):
        disks = await dm.discover_disks()

    os_disk_names = {d["device_name"] for d in disks if d["is_os_disk"]}
    assert "nvme0n1" in os_disk_names
    assert "nvme1n1" in os_disk_names


@pytest.mark.asyncio
async def test_read_slot_uuids_uses_partuuid_for_no_fs_partitions():
    """Partitions without a filesystem have a PARTUUID but a null UUID, so
    read_slot_uuids must key off PARTUUID or they silently disappear."""
    import json as _json
    from nazman.managers.disk_manager import read_slot_uuids

    lsblk_out = {
        "blockdevices": [
            {
                "name": "nvme0n1", "type": "disk", "partlabel": None,
                "partuuid": None, "size": "476.9G",
                "children": [
                    {"name": "nvme0n1p1", "type": "part", "partlabel": None,
                     "partuuid": "abc-1", "size": "1G"},
                    {"name": "nvme0n1p2", "type": "part", "partlabel": None,
                     "partuuid": "def-2", "size": "200G"},
                    {"name": "nvme0n1p3", "type": "part", "partlabel": None,
                     "partuuid": "ghi-3", "size": "275.9G"},
                ],
            },
            {
                "name": "nvme1n1", "type": "disk", "partlabel": None,
                "partuuid": None, "size": "465.8G",
                "children": [
                    {"name": "nvme1n1p4", "type": "part", "partlabel": None,
                     "partuuid": "jkl-4", "size": "264.8G"},
                ],
            },
        ]
    }

    async def fake_run_command(cmd, timeout=300, check=True, capture_output=True, input=None, **kwargs):
        return (_json.dumps(lsblk_out), "", 0)

    with patch("nazman.managers.disk_manager.run_command", side_effect=fake_run_command):
        result = await read_slot_uuids(["/dev/nvme0n1", "/dev/nvme1n1"])

    nvme0_parts = result["/dev/nvme0n1"]["partitions"]
    nvme1_parts = result["/dev/nvme1n1"]["partitions"]
    assert len(nvme0_parts) == 3
    assert nvme0_parts[2]["name"] == "nvme0n1p3"
    assert nvme0_parts[2]["slot_uuid"] == "ghi-3"
    assert len(nvme1_parts) == 1
    assert nvme1_parts[0]["name"] == "nvme1n1p4"
    assert nvme1_parts[0]["slot_uuid"] == "jkl-4"
