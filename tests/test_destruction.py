"""Tests for the pool/dataset destruction service (ZFS + NFS + SMB orchestration)."""

import pytest
from unittest.mock import patch, AsyncMock

from nazman.models.pool import Pool
from nazman.managers.zfs_manager import ZfsManager
from nazman.managers.nfs_manager import NfsManager
from nazman.managers.smb_manager import SmbManager
from nazman.services.destruction import DestructionService
from nazman.utils.exceptions import DatasetError, PoolError


@pytest.fixture()
def destruction():
    return DestructionService(zfs=ZfsManager(), nfs=NfsManager(), smb=SmbManager())


def _noop(*args, **kwargs):
    async def _f(*a, **k):
        return ("", "", 0)
    return _f


@pytest.mark.asyncio
async def test_destroy_pool_deletes_record(db_session, destruction):
    """destroy_pool should remove the Pool DB row."""
    pool = Pool(name="oldpool")
    db_session.add(pool)
    db_session.commit()

    with patch("nazman.services.destruction.run_zpool", _noop()), \
         patch("nazman.services.destruction.run_zfs", _noop()), \
         patch("nazman.services.destruction.run_command", _noop()):
        await destruction.destroy_pool(db_session, "oldpool")

    assert db_session.query(Pool).filter(Pool.name == "oldpool").count() == 0


@pytest.mark.asyncio
async def test_destroy_pool_unexports_nfs_before_destroy(db_session, destruction):
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

    with patch("nazman.services.destruction.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_run_zfs):

        await destruction.destroy_pool(db_session, "photolib1")

    assert db_session.query(Pool).filter(Pool.name == "photolib1").count() == 0

    # NFS unexport should disable sharenfs and unshare each pool dataset.
    off_sets = [c for c in zfs_calls if c[0] == "set" and "sharenfs=off" in c[1]]
    unshares = [c for c in zfs_calls if c[0] == "unshare"]
    assert any("photolib1" in c[2] for c in off_sets), zfs_calls
    assert any("photolib1/data" in c[2] for c in off_sets), zfs_calls
    assert len(unshares) >= 2, zfs_calls


@pytest.mark.asyncio
async def test_destroy_pool_blocks_on_mounted_datasets(db_session, destruction):
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

    with patch("nazman.services.destruction.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_command", side_effect=fake_run_command):

        with pytest.raises(PoolError, match="still mounted"):
            await destruction.destroy_pool(db_session, "dt")

    # Pool record left intact (destroy did not proceed).
    assert db_session.query(Pool).filter(Pool.name == "dt").count() == 1


@pytest.mark.asyncio
async def test_get_pool_destroy_info_reports_space_export_and_clients(db_session, destruction):
    """destroy-info should surface space used, active export, and connected clients."""
    pool = Pool(name="photolib1")
    db_session.add(pool)
    db_session.commit()

    async def fake_run_zpool(*args, **kwargs):
        if args[0] == "list":
            return ("photolib1\t3000000000000\t100000000000\t2900000000000", "", 0)
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        cmd = args[0]
        if cmd[0] == "showmount":
            return ("192.168.32.50:/photolib1\n192.168.32.51:/photolib1/media\n", "", 0)
        return ("", "", 0)

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("photolib1\nphotolib1/media\n", "", 0)
        # Any 'get sharenfs' returns a live share so the export is reported.
        return ("on", "", 0)

    with patch("nazman.services.destruction.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.utils.zfs_query.run_command", side_effect=fake_run_command), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_run_zfs):

        info = await destruction.get_pool_destroy_info(db_session, "photolib1")

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
async def test_destroy_dataset_runs_destroy(db_session, destruction):
    """destroy_dataset should issue a zfs destroy when there are no obstacles."""
    mock_run_zfs = AsyncMock(return_value=("", "", 0))
    with patch("nazman.services.destruction.run_zfs", mock_run_zfs), \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": False, "exports": [], "active_clients": [],
         })):
        await destruction.destroy_dataset(db_session, "tank/media")

    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls, "expected at least one zfs destroy call"
    assert any("tank/media" in c.args[1] for c in destroy_calls)


@pytest.mark.asyncio
async def test_destroy_dataset_blocked_when_mounted(db_session, destruction):
    """destroy_dataset must hard-block (no destroy) while the dataset is mounted."""
    async def fake_run_zfs(*args, **kwargs):
        return ("", "", 0)

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs) as mock_run_zfs, \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": True, "exports": [], "active_clients": [],
         })):
        with pytest.raises(DatasetError, match="still mounted"):
            await destruction.destroy_dataset(db_session, "tank/media")

    # No destroy command should have been issued.
    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls == []


@pytest.mark.asyncio
async def test_destroy_dataset_blocked_when_active_nfs_client(db_session, destruction):
    """destroy_dataset must hard-block while an NFS client holds the mount."""
    async def fake_run_zfs(*args, **kwargs):
        return ("", "", 0)

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs) as mock_run_zfs, \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": True, "exports": ["/tank/media"],
             "active_clients": [{"client": "192.168.1.10", "path": "/tank/media"}],
         })):
        with pytest.raises(DatasetError, match="NFS client"):
            await destruction.destroy_dataset(db_session, "tank/media")

    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls == []
