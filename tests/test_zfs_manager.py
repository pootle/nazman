import pytest
from unittest.mock import patch, AsyncMock

from nazman.models.pool import Pool
from nazman.managers.zfs_manager import zfs_manager


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
async def test_destroy_pool_deletes_record(db_session):
    """destroy_pool should remove the Pool DB row."""
    pool = Pool(name="oldpool")
    db_session.add(pool)
    db_session.commit()

    async def fake_run_zpool(*args, **kwargs):
        return ("", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool):
        await zfs_manager.destroy_pool(db_session, "oldpool")

    assert db_session.query(Pool).filter(Pool.name == "oldpool").count() == 0


@pytest.mark.asyncio
async def test_destroy_pool_unexports_nfs_before_destroy(db_session):
    """destroy_pool should unexport NFS shares belonging to the pool first."""
    pool = Pool(name="photolib1")
    db_session.add(pool)
    db_session.commit()

    zfs_calls = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        zfs_calls.append(cmd)
        # Enumerate the pool's child datasets.
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("photolib1\nphotolib1/data\n", "", 0)
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        return ("", "", 0)

    async def fake_run_zpool(*args, **kwargs):
        return ("", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.zfs_manager.run_command", side_effect=fake_run_command), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_run_zfs):

        await zfs_manager.destroy_pool(db_session, "photolib1")

    assert db_session.query(Pool).filter(Pool.name == "photolib1").count() == 0

    # NFS unexport should disable sharenfs and unshare each pool dataset.
    off_sets = [c for c in zfs_calls if c[0] == "set" and "sharenfs=off" in c[1]]
    unshares = [c for c in zfs_calls if c[0] == "unshare"]
    assert any("photolib1" in c[2] for c in off_sets), zfs_calls
    assert any("photolib1/data" in c[2] for c in off_sets), zfs_calls
    assert len(unshares) >= 2, zfs_calls


@pytest.mark.asyncio
async def test_destroy_pool_blocks_on_mounted_datasets(db_session):
    """destroy_pool should refuse when a child dataset is mounted, before any destroy runs."""
    pool = Pool(name="dt")
    db_session.add(pool)
    db_session.commit()

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        # Enumerate child datasets, then report dt/p1 as mounted.
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("dt\ndt/p1\n", "", 0)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        # showmount -a returns no connected clients
        return ("", "", 0)

    async def fake_run_zpool(*args, **kwargs):
        raise AssertionError("zpool destroy should not run when blocked")

    from nazman.utils.exceptions import PoolError

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.zfs_manager.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_manager.run_command", side_effect=fake_run_command):

        with pytest.raises(PoolError, match="still mounted"):
            await zfs_manager.destroy_pool(db_session, "dt")

    # Pool record left intact (destroy did not proceed).
    assert db_session.query(Pool).filter(Pool.name == "dt").count() == 1


@pytest.mark.asyncio
async def test_get_pool_destroy_info_reports_space_export_and_clients(db_session):
    """destroy-info should surface space used, active export, and connected clients."""
    pool = Pool(name="photolib1")
    db_session.add(pool)
    db_session.commit()

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "list":
            return ("photolib1\t3000000000000\t100000000000\t2900000000000", "", 0)
        return ("", "", 0)

    async def fake_run_command(cmd, timeout=300, check=True, capture_output=True, input=None):
        if cmd[0] == "showmount":
            return ("192.168.32.50:/photolib1\n192.168.32.51:/photolib1/media\n", "", 0)
        return ("", "", 0)

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("photolib1\nphotolib1/media\n", "", 0)
        # Any 'get sharenfs' returns a live share so the export is reported.
        return ("on", "", 0)

    with patch("nazman.managers.zfs_manager.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.managers.nfs_manager.run_command", side_effect=fake_run_command), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_run_zfs):

        info = await zfs_manager.get_pool_destroy_info(db_session, "photolib1")

    assert info["pool_name"] == "photolib1"
    assert info["size_bytes"] == 3000000000000
    assert info["used_bytes"] == 100000000000
    assert info["free_bytes"] == 2900000000000
    assert info["has_active_export"] is True
    export_paths = {e["export_path"] for e in info["exports"]}
    assert export_paths == {"/photolib1", "/photolib1/media"}
    assert any(e["export_path"] == "/photolib1/media" for e in info["exports"])
    clients = {c["client"] for c in info["active_clients"]}
    assert clients == {"192.168.32.50", "192.168.32.51"}


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
async def test_destroy_dataset_runs_destroy(db_session):
    """destroy_dataset should issue a zfs destroy when there are no obstacles."""
    async def fake_run_zfs(*args, **kwargs):
        return ("", "", 0)

    with patch("nazman.managers.zfs_manager.run_zfs", side_effect=fake_run_zfs) as mock_run_zfs, \
         patch.object(zfs_manager, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": False, "exports": [], "active_clients": [],
         })):
        await zfs_manager.destroy_dataset(db_session, "tank/media")

    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls, "expected at least one zfs destroy call"
    assert any("tank/media" in c.args[1] for c in destroy_calls)


@pytest.mark.asyncio
async def test_destroy_dataset_blocked_when_mounted(db_session):
    """destroy_dataset must hard-block (no destroy) while the dataset is mounted."""
    from nazman.utils.exceptions import DatasetError

    with patch.object(zfs_manager, "_dataset_destroy_obstacles", AsyncMock(return_value={
        "mounted": True, "exports": [], "active_clients": [],
    })), \
         patch("nazman.managers.zfs_manager.run_zfs", AsyncMock(return_value=("", "", 0))) as mock_run_zfs:
        with pytest.raises(DatasetError, match="still mounted"):
            await zfs_manager.destroy_dataset(db_session, "tank/media")

    # No destroy command should have been issued.
    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls == []


@pytest.mark.asyncio
async def test_destroy_dataset_blocked_when_active_nfs_client(db_session):
    """destroy_dataset must hard-block while an NFS client holds the mount. """
    from nazman.utils.exceptions import DatasetError

    with patch.object(zfs_manager, "_dataset_destroy_obstacles", AsyncMock(return_value={
        "mounted": False,
        "exports": [],
        "active_clients": [{"client": "192.168.1.10", "path": "/tank/media"}],
    })), \
         patch("nazman.managers.zfs_manager.run_zfs", AsyncMock(return_value=("", "", 0))) as mock_run_zfs:
        with pytest.raises(DatasetError, match="NFS client"):
            await zfs_manager.destroy_dataset(db_session, "tank/media")

    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls == []


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
    """ZfsManager.get_pool_status should call run_zpool with 'status -j'."""
    status_json = '{"pools":{"tank":{"state":"ONLINE","status":"","scan":{},"config":{"name":"tank","vdevs":[{"name":"stripe-0","type":"stripe","children":[{"name":"/dev/sda","state":"ONLINE"}]}]}}}}'
    with patch("nazman.managers.zfs_manager.run_zpool", new_callable=AsyncMock,
               return_value=(status_json, "", 0)) as mock:
        result = await zfs_manager.get_pool_status("tank")
    mock.assert_called_once_with("status", "-j", "tank", op="read")
    assert result["name"] == "tank"
    assert result["status"] == "ONLINE"


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
