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
async def test_destroy_pool_unmounts_mounted_children_then_destroys(db_session, destruction):
    """destroy_pool should not block on a mounted child: it unmounts then destroys."""
    pool = Pool(name="dt")
    db_session.add(pool)
    db_session.commit()

    calls = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        calls.append(("zfs", cmd))
        # Enumerate child datasets, then report dt/p1 as mounted.
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("dt\ndt/p1\n", "", 0)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        # showmount -a and smbstatus both report no clients.
        return ("", "", 0)

    async def fake_run_zpool(*args, **kwargs):
        calls.append(("zpool", list(args)))
        return ("", "", 0)

    with patch("nazman.services.destruction.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_command", side_effect=fake_run_command), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_run_zfs):

        await destruction.destroy_pool(db_session, "dt")

    assert db_session.query(Pool).filter(Pool.name == "dt").count() == 0

    # The pool root and its child were unmounted before zpool destroy.
    unmounts = [cmd for kind, cmd in calls if kind == "zfs" and cmd[0] == "unmount"]
    assert any(c[2] == "dt" for c in unmounts), calls
    assert any(c[2] == "dt/p1" for c in unmounts), calls
    destroys = [cmd for kind, cmd in calls if kind == "zpool" and cmd[0] == "destroy"]
    assert destroys == [["destroy", "-f", "dt"]], calls
    last_unmount_pos = max(i for i, (k, c) in enumerate(calls)
                           if k == "zfs" and c[0] == "unmount")
    destroy_pos = next(i for i, (k, c) in enumerate(calls)
                       if k == "zpool" and c[0] == "destroy")
    assert last_unmount_pos < destroy_pos, calls


@pytest.mark.asyncio
async def test_destroy_pool_still_blocks_on_active_nfs_client(db_session, destruction):
    """destroy_pool must still hard-block while an NFS client holds a child mount."""
    pool = Pool(name="dt")
    db_session.add(pool)
    db_session.commit()

    unmount_calls = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("dt\ndt/p1\n", "", 0)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)
        if cmd and cmd[0] == "unmount":
            unmount_calls.append(cmd)
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        # An NFS client still holds /dt/p1 (the probed child path).
        return ("192.168.1.10:/dt/p1\n", "", 0)

    async def fake_run_zpool(*args, **kwargs):
        raise AssertionError("zpool destroy should not run when a client is connected")

    with patch("nazman.services.destruction.run_zpool", side_effect=fake_run_zpool), \
         patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_command", side_effect=fake_run_command):

        with pytest.raises(PoolError, match="NFS client"):
            await destruction.destroy_pool(db_session, "dt")

    # Nothing torn down while a client is connected.
    assert unmount_calls == []
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
async def test_destroy_dataset_auto_unmounts_before_destroy(db_session, destruction):
    """destroy_dataset should not block on a mounted dataset: it unmounts then destroys."""
    calls = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        calls.append(cmd)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)
        if cmd and cmd[0] == "destroy":
            return ("", "", 0)
        return ("", "", 0)

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": True, "exports": [], "active_clients": [],
         })):
        await destruction.destroy_dataset(db_session, "tank/media")

    unmounts = [c for c in calls if c[0] == "unmount"]
    destroys = [c for c in calls if c[0] == "destroy"]
    assert unmounts == [["unmount", "-f", "tank/media"]], calls
    assert destroys == [["destroy", "tank/media"]], calls
    assert calls.index(unmounts[0]) < calls.index(destroys[0]), calls


@pytest.mark.asyncio
async def test_destroy_dataset_fails_cleanly_when_unmount_busy(db_session, destruction):
    """A local process holding the mount must surface as a clear error, not a destroy."""
    calls = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        calls.append(cmd)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)  # mounted: unmount is attempted next
        if cmd and cmd[0] == "unmount":
            return ("", "resource busy", 1)
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        return ("", "", 0)  # smbstatus/showmount report no sessions

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": True, "exports": [], "active_clients": [],
         })):
        with pytest.raises(DatasetError, match="Could not unmount") as exc_info:
            await destruction.destroy_dataset(db_session, "tank/media")

    assert "local process" in str(exc_info.value)
    destroy_calls = [c for c in calls if c[0] == "destroy"]
    assert destroy_calls == []


@pytest.mark.asyncio
async def test_dataset_destroy_obstacles_detects_live_smb_session(db_session, destruction):
    """Obstacles should report hosts with an open SMB connection to the dataset."""
    async def fake_run_command(*args, **kwargs):
        cmd = args[0]
        if cmd[0] == "smbstatus":
            return (
                "Samba version 4.15.13-Ubuntu\n\n"
                "Service      pid     Machine                                   Connected at\n"
                "-------------   -----   ------------------------------   --------------------------\n"
                "media         1234    192.168.33.139 (ipv4:192.168.33.139:51142)\n",
                "", 0,
            )
        return ("", "", 0)  # showmount -a: no NFS clients

    with patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch("nazman.services.destruction.run_zfs", AsyncMock(return_value=("", "", 0))), \
         patch.object(destruction.nfs, "read_sharenfs", AsyncMock(return_value="off")), \
         patch.object(destruction.smb, "list_shares", AsyncMock(return_value=[])):

        info = await destruction._dataset_destroy_obstacles(db_session, "tank/media")

    assert info["smb_connected"] == ["192.168.33.139"]
    assert info["active_clients"] == []


@pytest.mark.asyncio
async def test_destroy_dataset_blocks_with_network_drive_message(db_session, destruction):
    """An open SMB connection must hard-block before any destroy is attempted."""
    async def fake_run_zfs(*args, **kwargs):
        return ("", "", 0)

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs) as mock_run_zfs, \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": True, "exports": [], "active_clients": [],
             "smb_connected": ["192.168.33.139"],
         })):
        with pytest.raises(DatasetError, match="network drive") as exc_info:
            await destruction.destroy_dataset(db_session, "tank/media")

    assert "192.168.33.139" in str(exc_info.value)
    destroy_calls = [c for c in mock_run_zfs.call_args_list if c.args and c.args[0] == "destroy"]
    assert destroy_calls == []


@pytest.mark.asyncio
async def test_destroy_dataset_busy_unmount_names_network_drive(db_session, destruction):
    """A busy unmount caused by an SMB session should name the drive holder."""
    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)  # mounted: unmount is attempted
        if cmd and cmd[0] == "unmount":
            return ("", "pool or dataset is busy", 1)
        if cmd and cmd[0] == "destroy":
            raise AssertionError("destroy must not run when unmount fails")
        return ("", "", 0)

    async def fake_run_command(*args, **kwargs):
        cmd = args[0]
        if cmd[0] == "smbstatus":
            return (
                "Samba version 4.15.13-Ubuntu\n\n"
                "Service      pid     Machine\n"
                "media        1234    192.168.33.139 (ipv4:192.168.33.139:51142)\n",
                "", 0,
            )
        return ("", "", 0)  # showmount -a: no NFS clients

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.services.destruction.run_command", side_effect=fake_run_command), \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": True, "exports": [], "active_clients": [],
         })):
        with pytest.raises(DatasetError, match="network drive") as exc_info:
            await destruction.destroy_dataset(db_session, "tank/media")

    assert "192.168.33.139" in str(exc_info.value)
    assert "Could not unmount" in str(exc_info.value)


@pytest.mark.asyncio
async def test_destroy_dataset_recursive_unmounts_children(db_session, destruction):
    """Recursive destroy should unmount every child dataset before destroy -r."""
    calls = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        calls.append(cmd)
        if cmd and cmd[0] == "list" and "-r" in cmd:
            return ("tank\ntank/a\ntank/b\n", "", 0)
        if cmd and cmd[0] == "get":
            return ("yes", "", 0)
        return ("", "", 0)

    with patch("nazman.services.destruction.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch.object(destruction, "_dataset_destroy_obstacles", AsyncMock(return_value={
             "mounted": False, "exports": [], "active_clients": [],
         })):
        await destruction.destroy_dataset(db_session, "tank", recursive=True)

    unmounts = [c for c in calls if c[0] == "unmount"]
    destroyed = [c for c in calls if c[0] == "destroy"]
    assert {c[2] for c in unmounts} == {"tank", "tank/a", "tank/b"}, calls
    assert destroyed == [["destroy", "-r", "tank"]], calls
    last_unmount = calls.index(unmounts[-1])
    assert last_unmount < calls.index(destroyed[0]), calls


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
