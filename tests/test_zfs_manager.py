import pytest
from unittest.mock import patch, AsyncMock
import json

from nazman.models.pool import Pool
from nazman.models.disk import Disk
from nazman.managers.zfs_manager import ZfsManager

zfs_manager = ZfsManager()


REALISTIC_STATUS_JSON = '{"pools":{"photos1":{"state":"ONLINE","vdevs":{"photos1":{"name":"photos1","vdev_type":"root","class":"normal","state":"ONLINE","vdevs":{"mirror-0":{"name":"mirror-0","vdev_type":"mirror","class":"normal","state":"ONLINE","total_space":"1016G","vdevs":{"sdc":{"name":"sdc","vdev_type":"disk","class":"normal","state":"ONLINE"},"sdd":{"name":"sdd","vdev_type":"disk","class":"normal","state":"ONLINE"}}}}}},"special":{"special-0":{"name":"special-0","vdev_type":"mirror","class":"special","state":"ONLINE","total_space":"222G","vdevs":{"sda2":{"name":"sda2","vdev_type":"disk","class":"special","state":"ONLINE"},"sdb2":{"name":"sdb2","vdev_type":"disk","class":"special","state":"ONLINE"}}}},"log":{"log-0":{"name":"log-0","vdev_type":"mirror","class":"log","state":"ONLINE","total_space":"100G","vdevs":{"sda1":{"name":"sda1","vdev_type":"disk","class":"log","state":"ONLINE"},"sdb1":{"name":"sdb1","vdev_type":"disk","class":"log","state":"ONLINE"}}}}}}}'


@pytest.mark.asyncio
async def test_get_pool_status_parses_json():
    """Verify get_pool_status handles dict-keyed pools and config.vdevs."""
    async def fake_run_zpool(*args, **kwargs):
        return (REALISTIC_STATUS_JSON, "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("photos1")

    assert result["name"] == "photos1"
    assert result["status"] == "ONLINE"
    assert result["topology"] == "mirror"
    assert len(result["data_vdevs"]) == 1
    assert result["data_vdevs"][0]["name"] == "mirror-0"
    assert len(result["special_vdevs"]) == 1
    assert result["special_vdevs"][0]["name"] == "special-0"
    assert len(result["log_vdevs"]) == 1
    assert result["log_vdevs"][0]["name"] == "log-0"


@pytest.mark.asyncio
async def test_get_pool_members_parses_partitioned_by_id_vdevs():
    """zpool status -j on zfs>=2 reports vdev_type=disk, not type=disk, and
    partition members hold by-id -partN paths."""
    status_json = (
        '{"pools": {"allhdd": {"state": "ONLINE", "vdevs": {"allhdd": '
        '{"name": "allhdd", "vdev_type": "root", "vdevs": {"raidz1-0": '
        '{"name": "raidz1-0", "vdev_type": "raidz1", "vdevs": '
        '{"ata-X": {"name": "ata-X", "vdev_type": "disk", '
        '"path": "/dev/disk/by-id/ata-X-part1"}, '
        '"ata-Y": {"name": "ata-Y", "vdev_type": "disk", '
        '"path": "/dev/disk/by-id/ata-Y-part1"}}}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        return (status_json, "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        members = await zfs_manager.get_pool_members()

    assert members == {
        "/dev/disk/by-id/ata-X-part1": "allhdd",
        "/dev/disk/by-id/ata-Y-part1": "allhdd",
    }


@pytest.mark.asyncio
async def test_get_pool_status_root_direct_disks():
    """A simple stripe where the root directly holds bare disks must surface a data vdev."""
    status_json = (
        '{"pools":{"single":{"state":"ONLINE","vdevs":{"single":{'
        '"name":"single","vdev_type":"root","class":"root","state":"ONLINE",'
        '"vdevs":{"sda":{"name":"sda","vdev_type":"disk","state":"ONLINE",'
        '"path":"/dev/sda","rep_dev_size":"8.0T"},"sdb":{"name":"sdb","vdev_type":"disk",'
        '"state":"ONLINE","path":"/dev/sdb","rep_dev_size":"8.0T"}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        return (status_json, "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("single")

    assert len(result["data_vdevs"]) == 1
    assert result["data_vdevs"][0]["name"] == "single"
    names = [c["name"] for c in result["data_vdevs"][0]["children"]]
    assert names == ["sda", "sdb"]


@pytest.mark.asyncio
async def test_get_pool_status_raidz_type_normalized():
    """zpool status -j reports vdev_type 'raidz' generically; the parity count
    in the vdev name (raidz2-0) must surface as a concrete topology."""
    status_json = (
        '{"pools":{"tank":{"state":"ONLINE","vdevs":{"tank":{'
        '"name":"tank","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"raidz2-0":{"name":"raidz2-0","vdev_type":"raidz","class":"normal",'
        '"guid":"2001","state":"ONLINE","vdevs":{"sdc":{"name":"sdc",'
        '"vdev_type":"disk","class":"normal","guid":"2002","state":"ONLINE",'
        '"path":"/dev/sdc","rep_dev_size":"10.0T"},"sdd":{"name":"sdd",'
        '"vdev_type":"disk","class":"normal","guid":"2003","state":"ONLINE",'
        '"path":"/dev/sdd","rep_dev_size":"10.0T"},"sde":{"name":"sde",'
        '"vdev_type":"disk","class":"normal","guid":"2004","state":"ONLINE",'
        '"path":"/dev/sde","rep_dev_size":"10.0T"}}}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "status":
            return (status_json, "", 0)
        if args[3] == "name,property,value":
            return ("", "", 0)
        return ("12\n", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("tank")

    assert result["topology"] == "raidz2"
    assert result["data_vdevs"][0]["type"] == "raidz2"


@pytest.mark.asyncio
async def test_get_pool_status_mirror_type_stays_concrete():
    """Mirrors are already concrete in zpool status -j; normalization must not
    alter them."""
    status_json = (
        '{"pools":{"tank":{"state":"ONLINE","vdevs":{"tank":{'
        '"name":"tank","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"mirror-0":{"name":"mirror-0","vdev_type":"mirror","class":"normal",'
        '"state":"ONLINE","vdevs":{"sda":{"name":"sda","vdev_type":"disk",'
        '"state":"ONLINE","path":"/dev/sda"},"sdb":{"name":"sdb",'
        '"vdev_type":"disk","state":"ONLINE","path":"/dev/sdb"}}}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "status":
            return (status_json, "", 0)
        if args[3] == "name,property,value":
            return ("", "", 0)
        return ("12\n", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("tank")

    assert result["topology"] == "mirror"
    assert result["data_vdevs"][0]["type"] == "mirror"


@pytest.mark.asyncio
async def test_get_pool_status_attaches_physical_sector_size():
    """Leaf children carry their disk's physical sector size (ZFS ashift is the
    logical sector on 512e disks); groups carry the max of their members."""
    status_json = (
        '{"pools":{"tank":{"state":"ONLINE","vdevs":{"tank":{'
        '"name":"tank","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"raidz1-0":{"name":"raidz1-0","vdev_type":"raidz1","class":"normal",'
        '"state":"ONLINE","vdevs":{"sdc":{"name":"sdc","vdev_type":"disk",'
        '"state":"ONLINE","path":"/dev/sdc"},"sdd":{"name":"sdd",'
        '"vdev_type":"disk","state":"ONLINE","path":"/dev/sdd"}}}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "status":
            return (status_json, "", 0)
        if args[3] == "name,property,value":
            return ("", "", 0)
        return ("12\n", "", 0)

    def fake_phys_bytes(path_or_name):
        return 4096 if path_or_name in ("/dev/sdc", "/dev/sdd") else None

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.zfs_manager._physical_sector_bytes", side_effect=fake_phys_bytes):
        result = await zfs_manager.get_pool_status("tank")

    group = result["data_vdevs"][0]
    assert group["physical_sector_size"] == 4096


@pytest.mark.asyncio
async def test_get_pool_status_skips_physical_sector_when_absent():
    """Missing/unreadable devices leave physical_sector_size unset (no crash)."""
    status_json = (
        '{"pools":{"tank":{"state":"ONLINE","vdevs":{"tank":{'
        '"name":"tank","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"stripe-0":{"name":"stripe-0","vdev_type":"stripe","class":"normal",'
        '"state":"ONLINE","vdevs":{"sdc":{"name":"sdc","vdev_type":"disk",'
        '"state":"ONLINE","path":"/dev/nonexistent"}}}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "status":
            return (status_json, "", 0)
        if args[3] == "name,property,value":
            return ("", "", 0)
        return ("12\n", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.zfs_manager._physical_sector_bytes", return_value=None):
        result = await zfs_manager.get_pool_status("tank")

    group = result["data_vdevs"][0]
    assert group.get("physical_sector_size") in (None, "")
    assert group["children"][0].get("physical_sector_size") is None


@pytest.mark.asyncio
async def test_get_pool_status_data_pool_fallback():
    """Handle zpool status -j format where pool data lives under data.pool."""
    status_json = (
        '{"pool":{"name":"libx","state":"ONLINE","vdevs":{"libx":{'
        '"name":"libx","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"stripe-0":{"name":"stripe-0","vdev_type":"stripe","class":"data","state":"ONLINE","vdevs":{'
        '"sdc":{"name":"sdc","vdev_type":"disk","state":"ONLINE","path":"/dev/sdc",'
        '"rep_dev_size":"8.0T"},"sdd":{"name":"sdd","vdev_type":"disk","state":"ONLINE",'
        '"path":"/dev/sdd","rep_dev_size":"8.0T"}}}}}}},"status":"ONLINE"}'
    )

    async def fake_run_zpool(*args, **kwargs):
        return (status_json, "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("libx")

    assert result["status"] == "ONLINE"
    assert len(result["data_vdevs"]) == 1
    assert result["data_vdevs"][0]["type"] == "stripe"
    names = [c["name"] for c in result["data_vdevs"][0]["children"]]
    assert names == ["sdc", "sdd"]


@pytest.mark.asyncio
async def test_create_pool_recreates_after_stale_record(db_session):
    """create_pool should remove a stale DB row and proceed when ZFS pool is gone."""
    stale_pool = Pool(name="reusepool")
    db_session.add(stale_pool)
    db_session.commit()

    async def fake_run_zpool(*args, **kwargs):
        cmd = args[0]
        if cmd == "list":
            return ("", "", 1)
        if cmd == "create":
            return ("", "", 0)
        return ("", "", 0)

    # Mock disk lookup for _resolve_devices
    from nazman.models.disk import Disk
    disk = Disk(
                by_id="/dev/disk/by-id/ata-SSD_1", serial="SN1",
                size_bytes=5000000000, disk_type="nvme")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)

    async def fake_read_slot_uuids(paths):
        return {p: {"partitions": []} for p in paths}

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.zfs_manager.run_zfs", new_callable=AsyncMock,
               return_value=("", "", 0)), \
         patch("nazman.managers.zfs_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):

        pool = await zfs_manager.create_pool(
            db_session, name="reusepool",
            vdevs=[{"role": "data", "topology": "stripe", "devices": [{"disk_id": disk.id, "slot_uuid": None}]}],
        )

    assert db_session.query(Pool).filter(Pool.name == "reusepool").count() == 1


@pytest.mark.asyncio
async def test_create_pool_no_compression_in_zpool_cmd(db_session):
    """Compression is a dataset property: `zpool create` must not pass it and it must not be set on the pool root."""
    from nazman.models.disk import Disk

    disk = Disk(
                by_id="/dev/disk/by-id/ata-SSD_1", serial="SN1",
                size_bytes=5000000000, disk_type="hdd")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)

    async def fake_read_slot_uuids(paths):
        return {p: {"partitions": []} for p in paths}

    with patch("nazman.managers.zfs_manager.run_zpool", new_callable=AsyncMock,
               return_value=("", "", 0)) as mock_zpool, \
         patch("nazman.managers.zfs_manager.run_zfs", new_callable=AsyncMock,
               return_value=("", "", 0)) as mock_zfs, \
         patch("nazman.managers.zfs_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):

        await zfs_manager.create_pool(
            db_session, name="newpool",
            vdevs=[{"role": "data", "topology": "stripe", "devices": [{"disk_id": disk.id, "slot_uuid": None}]}],
        )

    zpool_args = mock_zpool.call_args[0]
    assert "-f" in zpool_args, f"-f not found in zpool args: {zpool_args}"
    assert "compression" not in zpool_args, f"compression found in zpool args: {zpool_args}"

    # No `zfs set compression` should be issued on the pool root either.
    zfs_calls = mock_zfs.call_args_list
    compression_sets = [c for c in zfs_calls if c.args and c.args[0] == "set" and "compression=" in (c.args[1] if len(c.args) > 1 else "")]
    assert compression_sets == [], f"compression was set on pool root: {compression_sets}"


@pytest.mark.asyncio
async def test_create_pool_applies_special_vdev(db_session):
    """Verify special vdevs are included in the zpool create command."""
    from nazman.models.disk import Disk

    disks = []
    for name in ("sda", "sdb"):
        d = Disk(
                 by_id=f"/dev/disk/by-id/ata-{name}_1", serial=f"SN_{name}",
                 size_bytes=5000000000, disk_type="nvme")
        db_session.add(d)
        disks.append(d)
    db_session.commit()
    for d in disks:
        db_session.refresh(d)

    async def fake_read_slot_uuids(paths):
        return {p: {"partitions": []} for p in paths}

    with patch("nazman.managers.zfs_manager.run_zpool", new_callable=AsyncMock,
               return_value=("", "", 0)) as mock_zpool, \
         patch("nazman.managers.zfs_manager.run_zfs", new_callable=AsyncMock,
               return_value=("", "", 0)), \
         patch("nazman.managers.zfs_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):

        await zfs_manager.create_pool(
            db_session, name="mypoool",
            vdevs=[
                {"role": "data", "topology": "mirror", "devices": [
                    {"disk_id": disks[0].id, "slot_uuid": None},
                    {"disk_id": disks[1].id, "slot_uuid": None},
                ]},
                {"role": "special", "topology": "stripe", "devices": [
                    {"disk_id": disks[0].id, "slot_uuid": None},
                ]},
            ],
        )

    zpool_args = mock_zpool.call_args[0]
    assert "special" in zpool_args, f"'special' keyword not found in zpool args: {zpool_args}"
    special_idx = zpool_args.index("special")
    pool_name_idx = zpool_args.index("mypoool")
    assert special_idx > pool_name_idx, "special must come after pool name"


@pytest.mark.asyncio
async def test_create_pool_per_vdev_ashift(db_session):
    """Groups with a differing ashift are added via zpool add after creating the pool."""
    from nazman.models.disk import Disk

    disks = []
    for name in ("sda", "sdb", "sdc"):
        d = Disk(
                 by_id=f"/dev/disk/by-id/ata-{name}_1", serial=f"SN_{name}",
                 size_bytes=5000000000, disk_type="nvme")
        db_session.add(d)
        disks.append(d)
    db_session.commit()
    for d in disks:
        db_session.refresh(d)

    async def fake_read_slot_uuids(paths):
        return {p: {"partitions": []} for p in paths}

    with patch("nazman.managers.zfs_manager.run_zpool", new_callable=AsyncMock,
               return_value=("", "", 0)) as mock_zpool, \
         patch("nazman.managers.zfs_manager.run_zfs", new_callable=AsyncMock,
               return_value=("", "", 0)), \
         patch("nazman.managers.zfs_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):

        await zfs_manager.create_pool(
            db_session, name="newpool", ashift=12,
            vdevs=[
                {"role": "data", "topology": "stripe", "devices": [
                    {"disk_id": disks[0].id, "slot_uuid": None},
                ]},
                {"role": "special", "topology": "stripe", "ashift": 9, "devices": [
                    {"disk_id": disks[1].id, "slot_uuid": None},
                ]},
                {"role": "log", "topology": "stripe", "devices": [
                    {"disk_id": disks[2].id, "slot_uuid": None},
                ]},
            ],
        )

    # Two separate run_zpool invocations: one create, one add.
    calls = [c.args for c in mock_zpool.call_args_list]
    assert len(calls) == 2, f"expected create+add, got: {calls}"

    create_args, add_args = calls

    # Create step: single global ashift, data + inheriting log vdev only.
    assert create_args[0] == "create"
    assert "-o" in create_args and "ashift=12" in create_args
    assert "special" not in create_args, "differing-ashift special must NOT be in create"
    assert "log" in create_args, "inheriting log vdev stays in create"
    # Only a single -o ashift in the create command.
    assert sum(1 for a in create_args if a.startswith("ashift=")) == 1

    # Add step: special added with its own ashift right after the role keyword.
    assert add_args[0] == "add"
    assert "ashift=9" in add_args
    assert sum(1 for a in add_args if a.startswith("ashift=")) == 1
    special_idx = add_args.index("special")
    assert add_args[special_idx + 1] == "mirror" or add_args[special_idx + 1].startswith("/dev/disk")


@pytest.mark.asyncio
async def test_create_pool_log_vdev(db_session):
    """Verify log vdevs are included in the zpool create command."""
    from nazman.models.disk import Disk

    disks = []
    for name in ("sda", "sdb"):
        d = Disk(
                 by_id=f"/dev/disk/by-id/ata-{name}_1", serial=f"SN_{name}",
                 size_bytes=5000000000, disk_type="nvme")
        db_session.add(d)
        disks.append(d)
    db_session.commit()
    for d in disks:
        db_session.refresh(d)

    async def fake_read_slot_uuids(paths):
        return {p: {"partitions": []} for p in paths}

    with patch("nazman.managers.zfs_manager.run_zpool", new_callable=AsyncMock,
               return_value=("", "", 0)) as mock_zpool, \
         patch("nazman.managers.zfs_manager.run_zfs", new_callable=AsyncMock,
               return_value=("", "", 0)), \
         patch("nazman.managers.zfs_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):

        await zfs_manager.create_pool(
            db_session, name="tank",
            vdevs=[
                {"role": "data", "topology": "stripe", "devices": [
                    {"disk_id": disks[0].id, "slot_uuid": None},
                ]},
                {"role": "log", "topology": "mirror", "devices": [
                    {"disk_id": disks[0].id, "slot_uuid": None},
                    {"disk_id": disks[1].id, "slot_uuid": None},
                ]},
            ],
        )

    zpool_args = mock_zpool.call_args[0]
    assert "log" in zpool_args, f"'log' keyword not found in zpool args: {zpool_args}"
    log_idx = zpool_args.index("log")
    pool_name_idx = zpool_args.index("tank")
    assert log_idx > pool_name_idx, "log must come after pool name"




@pytest.mark.asyncio
async def test_create_dataset_runs_zfs_and_returns_name(db_session):
    """create_dataset should issue zfs create and return the name-keyed result."""
    pool = Pool(name="tank")
    db_session.add(pool)
    db_session.commit()

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            # No existing dataset (clean create path).
            return ("", "", 1)
        if cmd and cmd[0] == "create":
            return ("", "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_manager.run_zfs", side_effect=fake_run_zfs):
        result = await zfs_manager.create_dataset(
            db_session, name="media", pool_name="tank",
            compression="zstd", recordsize="128K", sync_mode="standard"
        )

    assert result["name"] == "tank/media"


@pytest.mark.asyncio
async def test_create_dataset_passes_special_small_blocks(db_session):
    """create_dataset should pass special_small_blocks as a zfs create -o option."""
    pool = Pool(name="tank")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    create_args_captured = {}

    async def fake_run_zfs(*args, **kwargs):
        cmd = args[0]
        if cmd == "list":
            return ("", "", 1)
        if cmd == "create":
            create_args_captured.update({"args": args[1:]})
            return ("", "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_manager.run_zfs", side_effect=fake_run_zfs):
        result = await zfs_manager.create_dataset(
            db_session, name="pics", pool_name="tank",
            compression="zstd", recordsize="128K", sync_mode="standard",
            special_small_blocks="64K"
        )

    call_args = create_args_captured["args"]
    assert "special_small_blocks=64K" in call_args
    assert result["special_small_blocks"] == "64K"


@pytest.mark.asyncio
async def test_create_dataset_omits_special_small_blocks_when_unset(db_session):
    """create_dataset should not pass special_small_blocks when it is None."""
    pool = Pool(name="tank")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    create_args_captured = {}

    async def fake_run_zfs(*args, **kwargs):
        cmd = args[0]
        if cmd == "list":
            return ("", "", 1)
        if cmd == "create":
            create_args_captured.update({"args": args[1:]})
            return ("", "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_manager.run_zfs", side_effect=fake_run_zfs):
        result = await zfs_manager.create_dataset(
            db_session, name="media", pool_name="tank",
            compression="zstd", recordsize="128K", sync_mode="standard"
        )

    call_args = create_args_captured["args"]
    assert not any("special_small_blocks" in a for a in call_args)
    assert result["special_small_blocks"] == "0"


@pytest.mark.asyncio
async def test_get_pool_status_delegates_to_run_zpool():
    """ZfsManager.get_pool_status should call run_zpool with 'status -j' and
    the per-vdev ashift query."""
    status_json = '{"pools":{"tank":{"state":"ONLINE","status":"","scan":{},"config":{"name":"tank","vdevs":[{"name":"stripe-0","type":"stripe","children":[{"name":"/dev/sda","state":"ONLINE"}]}]}}}}'
    with patch("nazman.managers.zfs_manager.run_zpool", new_callable=AsyncMock,
               return_value=(status_json, "", 0)) as mock:
        result = await zfs_manager.get_pool_status("tank")
    mock.assert_any_call("status", "-j", "tank", op="read")
    mock.assert_any_call("get", "-Hp", "-o", "name,property,value", "ashift,guid",
                         "tank", "all-vdevs", check=False, op="read")
    assert result["name"] == "tank"
    assert result["status"] == "ONLINE"


@pytest.mark.asyncio
async def test_get_pool_status_attaches_vdev_ashift():
    """Per-vdev ashift from 'zpool get ... all-vdevs' must land on groups and
    leaves (matched via guid), with pool-level ashift/sector size attached."""
    status_json = (
        '{"pools":{"tank":{"state":"ONLINE","vdevs":{"tank":{'
        '"name":"tank","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"raidz1-0":{"name":"raidz1-0","vdev_type":"raidz1","class":"normal",'
        '"guid":"2001","state":"ONLINE","vdevs":{"sdc":{"name":"sdc",'
        '"vdev_type":"disk","class":"normal","guid":"2002","state":"ONLINE",'
        '"path":"/dev/sdc"},"sdd":{"name":"sdd","vdev_type":"disk",'
        '"class":"normal","guid":"2003","state":"ONLINE",'
        '"path":"/dev/sdd"}}}}}}}}}'
    )
    all_vdevs_out = (
        "root-0\tashift\t12\nroot-0\tguid\t1000\n"
        "raidz1-0\tashift\t12\nraidz1-0\tguid\t2001\n"
        "sdc\tashift\t9\nsdc\tguid\t2002\n"
        "sdd\tashift\t9\nsdd\tguid\t2003\n"
    )

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "status":
            return (status_json, "", 0)
        if args[3] == "name,property,value":
            return (all_vdevs_out, "", 0)
        return ("12\n", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("tank")

    group = result["data_vdevs"][0]
    assert group["name"] == "raidz1-0"
    assert group["ashift"] == 12
    by_name = {c["name"]: c for c in group["children"]}
    assert by_name["sdc"]["ashift"] == 9
    assert by_name["sdd"]["ashift"] == 9
    assert result["ashift"] == 12
    assert result["sector_size_bytes"] == 4096


@pytest.mark.asyncio
async def test_get_pool_status_handles_missing_all_vdevs():
    """When 'zpool get ... all-vdevs' is unavailable (rc != 0, old ZFS), status
    still loads and per-vdev ashift stays unset without crashing."""
    status_json = (
        '{"pools":{"tank":{"state":"ONLINE","vdevs":{"tank":{'
        '"name":"tank","vdev_type":"root","class":"root","state":"ONLINE","vdevs":{'
        '"stripe-0":{"name":"stripe-0","vdev_type":"stripe","class":"normal",'
        '"state":"ONLINE","vdevs":{"sdc":{"name":"sdc","vdev_type":"disk",'
        '"state":"ONLINE","path":"/dev/sdc"}}}}}}}}}'
    )

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "status":
            return (status_json, "", 0)
        if args[3] == "name,property,value":
            return ("", "bad request", 1)
        return ("12\n", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        result = await zfs_manager.get_pool_status("tank")

    group = result["data_vdevs"][0]
    assert group.get("ashift") is None
    assert group["children"][0].get("ashift") is None
    assert result["ashift"] == 12


@pytest.mark.asyncio
async def test_list_pools_parses_raw_size(db_session):
    """Verify list_pools parses raw size bytes from zpool list -P."""
    pool_output = "tank\t4398046511104\t1099511627776\t3298534883328\t75\t-"

    async def fake_run_zpool(*args, **kwargs):
        return (pool_output, "", 0)

    async def fake_run_zfs(*args, **kwargs):
        return ("", "", 0)

    async def fake_get_status(name):
        return {"status": "ONLINE", "topology": "stripe"}

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.zfs_manager.run_zfs", side_effect=fake_run_zfs), \
         patch.object(zfs_manager, "get_pool_status", side_effect=fake_get_status):

        pools = await zfs_manager.list_pools(db_session)

    assert len(pools) == 1
    assert pools[0]["name"] == "tank"
    assert pools[0]["size_bytes"] == 4398046511104


@pytest.mark.asyncio
async def test_create_pool_requires_data_vdev(db_session):
    """create_pool should fail if no data vdev is provided."""
    from nazman.models.disk import Disk
    from nazman.utils.exceptions import ValidationError

    disk = Disk(
                by_id="/dev/disk/by-id/ata-SSD_1", serial="SN1",
                size_bytes=5000000000, disk_type="nvme")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)

    with pytest.raises(ValidationError, match="At least one data vdev"):
        await zfs_manager.create_pool(
            db_session, name="badpool",
            vdevs=[{"role": "log", "topology": "stripe", "devices": [{"disk_id": disk.id, "slot_uuid": None}]}],
        )


@pytest.mark.asyncio
async def test_create_pool_rejects_invalid_role(db_session):
    """create_pool should fail with an invalid vdev role."""
    from nazman.models.disk import Disk
    from nazman.utils.exceptions import ValidationError

    disk = Disk(
                by_id="/dev/disk/by-id/ata-SSD_1", serial="SN1",
                size_bytes=5000000000, disk_type="nvme")
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)

    with pytest.raises(ValidationError, match="Invalid vdev role"):
        await zfs_manager.create_pool(
            db_session, name="badpool",
            vdevs=[{"role": "bogus", "topology": "stripe", "devices": [{"disk_id": disk.id, "slot_uuid": None}]}],
        )


@pytest.mark.asyncio
async def test_get_pool_error_counts_parses_leaves():
    """get_pool_error_counts should read read/write/cksum counters per leaf."""
    status_json = json.dumps({
        "pools": {
            "data1": {
                "state": "ONLINE",
                "vdevs": {
                    "data1": {
                        "name": "data1", "vdev_type": "root",
                        "vdevs": {
                            "mirror-0": {
                                "name": "mirror-0", "vdev_type": "mirror",
                                "vdevs": {
                                    "ata-X": {
                                        "name": "ata-X", "vdev_type": "disk",
                                        "path": "/dev/disk/by-id/ata-X-part1",
                                        "read": 3, "write": 5, "cksum": 7, "guid": "1234",
                                    },
                                    "ata-Y": {
                                        "name": "ata-Y", "vdev_type": "disk",
                                        "path": "/dev/disk/by-id/ata-Y-part1",
                                        "read": 0, "write": 0, "cksum": 0, "guid": "5678",
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    })

    async def fake_run_zpool(*args, **kwargs):
        return (status_json, "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        counts = await zfs_manager.get_pool_error_counts()

    assert counts == {
        "/dev/disk/by-id/ata-X-part1": {
            "pool": "data1", "read": 3, "write": 5, "cksum": 7, "guid": "1234",
        },
        "/dev/disk/by-id/ata-Y-part1": {
            "pool": "data1", "read": 0, "write": 0, "cksum": 0, "guid": "5678",
        },
    }


@pytest.mark.asyncio
async def test_get_pool_error_counts_returns_empty_on_failure():
    async def fake_run_zpool(*args, **kwargs):
        return ("", "boom", 1)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        counts = await zfs_manager.get_pool_error_counts()

    assert counts == {}


def test_pool_errors_for_disk_aggregates_partition_children():
    counts = {
        "/dev/disk/by-id/ata-X-part1": {"read": 3, "write": 5, "cksum": 7},
        "/dev/disk/by-id/ata-X-part2": {"read": 1, "write": 0, "cksum": 2},
        "/dev/disk/by-id/ata-OTHER-part1": {"read": 99, "write": 99, "cksum": 99},
    }
    disk = Disk(by_id="/dev/disk/by-id/ata-X", model="M", size_bytes=1, disk_type="hdd")
    totals = zfs_manager.pool_errors_for_disk(counts, disk)

    assert totals == {"read": 4, "write": 5, "cksum": 9}


def test_pool_errors_for_disk_returns_none_when_not_in_pool():
    counts = {"/dev/disk/by-id/ata-X-part1": {"read": 3, "write": 5, "cksum": 7}}
    disk = Disk(by_id="/dev/disk/by-id/ata-Z", model="M", size_bytes=1, disk_type="hdd")
    assert zfs_manager.pool_errors_for_disk(counts, disk) is None


def test_leaf_identities_for_disk_guid_and_path():
    counts = {
        "/dev/disk/by-id/ata-X-part1": {"guid": "1234"},
        "/dev/disk/by-id/ata-OTHER-part1": {"guid": "9999"},
    }
    disk = Disk(by_id="/dev/disk/by-id/ata-X", model="M", size_bytes=1, disk_type="hdd")
    identities = zfs_manager.leaf_identities_for_disk(counts, disk)

    assert identities["guids"] == {"1234"}
    assert identities["paths"] == {"/dev/disk/by-id/ata-X-part1"}


@pytest.mark.asyncio
async def test_get_pool_error_events_parses_verbose_text():
    events_output = (
        "2025-01-02T03:04:05.123456000Z\tereport.fs.zfs.checksum\n"
        "    class = ereport.fs.zfs.checksum\n"
        "    pool = data1\n"
        "    vdev_guid = 1234\n"
        "    vdev_path = /dev/disk/by-id/ata-X-part1\n"
        "2025-01-02T03:04:06.987654321Z\tereport.fs.zfs.io_failure\n"
        "    class = ereport.fs.zfs.io_failure\n"
        "    pool = data1\n"
        "    vdev_guid = 5678\n"
        "    vdev_path = /dev/disk/by-id/ata-Y-part1\n"
        "2025-01-02T03:04:07.000000000Z\tereport.fs.zfs.pool.create\n"
        "    class = ereport.fs.zfs.pool.create\n"
        "    pool = data1\n"
    )

    async def fake_run_zpool(*args, **kwargs):
        return (events_output, "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        events = await zfs_manager.get_pool_error_events("data1")

    # Only checksum/io error classes survive; pool.create is filtered out.
    assert [e["class"] for e in events] == [
        "ereport.fs.zfs.checksum", "ereport.fs.zfs.io_failure",
    ]
    assert events[0]["vdev_guid"] == "1234"
    assert events[0]["vdev_path"] == "/dev/disk/by-id/ata-X-part1"
    assert events[1]["vdev_path"] == "/dev/disk/by-id/ata-Y-part1"


@pytest.mark.asyncio
async def test_get_pool_error_events_returns_empty_on_failure():
    async def fake_run_zpool(*args, **kwargs):
        return ("", "no such pool", 1)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        events = await zfs_manager.get_pool_error_events("data1")

    assert events == []


def test_events_for_disk_matches_guid_or_path_newest_first():
    identities = {
        "guids": {"1234"},
        "paths": {"/dev/disk/by-id/ata-X-part1"},
    }
    events = [
        {"time": "2025-01-02T03:04:05Z", "class": "ereport.fs.zfs.checksum",
         "vdev_guid": "1234", "vdev_path": "/dev/disk/by-id/ata-X-part1"},
        {"time": "2025-01-02T03:04:06Z", "class": "ereport.fs.zfs.checksum",
         "vdev_guid": "9999", "vdev_path": "/dev/sdb"},
        {"time": "2025-01-02T03:04:07Z", "class": "ereport.fs.zfs.checksum",
         "vdev_guid": "7777", "vdev_path": "/dev/disk/by-id/ata-X-part1"},
    ]
    matched = zfs_manager.events_for_disk(events, identities)

    assert [e["time"] for e in matched] == [
        "2025-01-02T03:04:07Z", "2025-01-02T03:04:05Z",
    ]


@pytest.mark.asyncio
async def test_get_pool_recreate_specs_includes_partition_geometry(db_session):
    """A rebuild needs the slot UUID *and* partition size/number for vdevs that
    sit on partitions, so the GPT layout can be reproduced."""
    disk = Disk(by_id="/dev/disk/by-id/ata-X", serial="SX",
                size_bytes=1000, disk_type="hdd")
    db_session.add(disk)
    db_session.commit()

    status = {"data_vdevs": [{
        "name": "raidz1-0", "type": "raidz1",
        "children": [{"name": "ata-X", "path": "/dev/disk/by-id/ata-X-part1"}],
    }]}
    slot_map = {
        "/dev/disk/by-id/ata-X-part1": {
            "slot_uuid": "slot-1", "size_bytes": 123456, "partition_number": 1,
        },
    }

    with patch.object(zfs_manager, "list_pool_names", return_value=["tank"]), \
         patch.object(zfs_manager, "get_pool_status", new_callable=AsyncMock, return_value=status), \
         patch.object(zfs_manager, "_get_pool_ashift", new_callable=AsyncMock, return_value=12), \
         patch.object(zfs_manager, "_slot_uuid_map", new_callable=AsyncMock, return_value=slot_map):
        specs = await zfs_manager.get_pool_recreate_specs(db_session)

    assert specs[0]["name"] == "tank"
    assert specs[0]["ashift"] == 12
    assert specs[0]["vdevs"][0]["topology"] == "raidz1"
    device = specs[0]["vdevs"][0]["devices"][0]
    assert device["by_id"] == "/dev/disk/by-id/ata-X"
    assert device["slot_uuid"] == "slot-1"
    assert device["partition_number"] == 1
    assert device["partition_size_bytes"] == 123456
