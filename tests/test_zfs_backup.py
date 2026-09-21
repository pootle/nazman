import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from nazman.models.disk import Disk
from nazman.models.pool import Pool
from nazman.models.backup_zfs import BackupDisk, BackupRun, BackupSchedule
from nazman.managers.zfs_backup_manager import ZfsBackupManager
from nazman.managers.zfs_manager import ZfsManager
from nazman.managers.scheduler import SchedulerManager

zfs_manager = ZfsManager()
scheduler_manager = SchedulerManager()
zfs_backup_manager = ZfsBackupManager(zfs=zfs_manager, scheduler=scheduler_manager)
from nazman.utils.exceptions import ValidationError, BackupError, CommandError
from nazman.wiring import get_zfs_backup_manager
from tests.conftest import override_manager


def _mk_pool(db_session, name):
    pool = Pool(name=name)
    db_session.add(pool)
    db_session.flush()
    return pool


@pytest.mark.asyncio
async def test_run_backup_full_writes_successful_run(db_session, tmp_path, monkeypatch):
    bd = BackupDisk(
        disk_id=999, mount_point=str(tmp_path), fs_uuid="AAA",
        unmount_after_backup=True,
    )
    db_session.add(bd)
    db_session.commit()

    snap_name = "tank/media@backup-20260901-000000"
    stream_file = str(tmp_path / "data/tank/media/full-20260901-000000.zfs.gz")
    os.makedirs(os.path.dirname(stream_file), exist_ok=True)

    mounted = [True]
    cmds = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "snapshot":
            return ("", "", 0)
        if cmd and cmd[0] == "destroy":
            return ("", "", 0)
        if cmd and cmd[0] == "get":
            return ("123456", "", 0)
        if cmd and cmd[0] == "list":
            # For the existence check, list the dataset itself; snapshot
            # listings (-t snapshot) still return nothing so no anchor exists.
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        assert stages[0][:3] == ["zfs", "send", "-R"]
        assert stages[1][0] == "gzip"
        os.makedirs(os.path.dirname(stdout_path), exist_ok=True)
        with open(stdout_path, "w") as f:
            f.write("STREAMSIM")
        return ("", "", 0)

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[0] == "mount":
            mounted[0] = True
        if cmd[0] == "umount":
            mounted[0] = False
        return ("", "", 0)

    def fake_is_mount(self):
        return mounted[0] and str(self) == str(tmp_path)

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs) as rzfs, \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline) as rpipe, \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        run = await zfs_backup_manager.run_backup(
            db_session, dataset_name="tank/media", backup_disk_id=bd.id, backup_type="full"
        )

    assert run.status == "success"
    assert run.backup_type == "full"
    assert run.snapshot.startswith("tank/media@backup-")
    assert run.stream_file.endswith(".zfs.gz")
    assert run.size_bytes == len("STREAMSIM")
    assert run.dataset_name == "tank/media"

    # Idle-unmount is the default: after a successful backup the drive is
    # unmounted again.
    assert not Path(tmp_path).is_mount()
    assert ("umount", str(tmp_path)) in [tuple(c) for c in cmds]

    # A self-describing manifest + sidecar are written for the volume.
    from nazman.utils import backup_manifest as bm
    manifest = bm.load_manifest(tmp_path)
    assert manifest is not None
    assert manifest["datasets"][0]["name"] == "tank/media"
    run_entry = manifest["datasets"][0]["backups"][0]
    assert run_entry["sha256"] == run.sha256
    assert run_entry["stream_file"] == str(Path(run.stream_file).relative_to(tmp_path))
    sidecar = bm.read_sidecar(run.stream_file)
    assert sidecar["kind"] == "dataset"
    assert sidecar["run"]["sha256"] == run.sha256


@pytest.mark.asyncio
async def test_run_backup_capacity_insufficient_aborts(db_session, tmp_path, monkeypatch):
    bd = BackupDisk(
        disk_id=999, mount_point=str(tmp_path), fs_uuid="AAA",
    )
    db_session.add(bd)
    db_session.commit()

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "snapshot":
            return ("", "", 0)
        if cmd and cmd[0] == "destroy":
            return ("", "", 0)
        if cmd and cmd[0] == "get":
            return ("123456", "", 0)
        if cmd and cmd[0] == "list":
            return ("tank/media", "", 0)
        return ("", "", 0)

    class FakeStatvfs:
        f_frsize = 1024
        f_blocks = 1024
        f_bavail = 1  # capacity guard: needed (used * margin) far exceeds this

    monkeypatch.setattr(Path, "is_mount", lambda self: str(self) == str(tmp_path))

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.os.statvfs", return_value=FakeStatvfs()), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", new=AsyncMock()):
        run = await zfs_backup_manager.run_backup(
            db_session, dataset_name="tank/media", backup_disk_id=bd.id, backup_type="full"
        )

    assert run.status == "failed"
    assert "Insufficient free space" in (run.error or "")


@pytest.mark.asyncio
async def test_estimate_capacity(db_session):
    with patch.object(zfs_backup_manager, "estimate_full_size", new=AsyncMock(return_value=1000)):
        needed = await zfs_backup_manager.estimate_needed("tank/media")
    assert needed == int(1000 * zfs_backup_manager.settings.backup_full_margin)


@pytest.mark.asyncio
async def test_api_list_backup_disks_empty(client, db_session):
    response = client.get("/api/backup-zfs/disks")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_list_backup_runs(client, db_session):
    response = client.get("/api/backup-zfs/runs")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_run_backup(client, db_session):
    bd = BackupDisk(
        disk_id=999, mount_point="/tmp/mnt", fs_uuid="AAA",
    )
    db_session.add(bd)
    db_session.commit()

    run = BackupRun(
        dataset_name="tank/media", backup_disk_id=bd.id,
        backup_type="full", stream_file="/tmp/mnt/full.zfs.gz",
        snapshot="tank/media@backup-x", status="success", size_bytes=10,
    )
    db_session.add(run)
    db_session.commit()

    with override_manager(get_zfs_backup_manager) as mock:
        mock.start_run_backup = AsyncMock(return_value=run)
        response = client.post("/api/backup-zfs/runs", json={
            "dataset_name": "tank/media", "backup_disk_id": bd.id, "backup_type": "full",
        })
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["dataset_name"] == "tank/media"
    assert data["status"] == "success"


@pytest.mark.asyncio
async def test_api_used_disks_empty(client, db_session):
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})):
        response = client.get("/api/backup-zfs/disks/used")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_used_disks_lists_whole_and_partition(client, db_session):
    whole_disk = Disk(
        by_id="/dev/disk/by-id/usb-CAND", model="USB", serial="CAND1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(whole_disk)
    db_session.commit()
    part_disk = Disk(
        by_id="/dev/disk/by-id/usb-OTHER", model="USB", serial="OTH1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(part_disk)
    db_session.commit()
    db_session.add_all([
        BackupDisk(disk_id=whole_disk.id, mount_point="/tmp/mnt1", fs_uuid="UUU1"),
        BackupDisk(disk_id=part_disk.id, slot_uuid="slot-abc",
                   mount_point="/tmp/mnt2", fs_uuid="UUU2"),
    ])
    db_session.commit()

    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})):
        response = client.get("/api/backup-zfs/disks/used")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {"disk_id": whole_disk.id, "slot_uuid": None} in rows
    assert {"disk_id": part_disk.id, "slot_uuid": "slot-abc"} in rows


@pytest.mark.asyncio
async def test_api_used_disks_includes_pool_members(client, db_session):
    part_disk = Disk(
        by_id="/dev/disk/by-id/sata-PART", model="HDD", serial="PT1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(part_disk)
    db_session.commit()
    whole_disk = Disk(
        by_id="/dev/disk/by-id/sata-WHOLE", model="HDD", serial="WH1",
        size_bytes=10**12, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(whole_disk)
    db_session.commit()

    members = {
        "/dev/disk/by-id/sata-PART-part2": "poolA",
        "/dev/disk/by-id/sata-WHOLE": "poolB",
    }
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value=members)), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdc"):
        response = client.get("/api/backup-zfs/disks/used")

    assert response.status_code == 200, response.text
    rows = response.json()
    # A partition member claims the whole disk just like a whole-disk member.
    assert {"disk_id": part_disk.id, "slot_uuid": None} in rows
    assert {"disk_id": whole_disk.id, "slot_uuid": None} in rows


async def _add_declare_disk(db, by_id="/dev/disk/by-id/ata-X"):
    disk = Disk(by_id=by_id, model="HDD", serial="X1", size_bytes=10**11,
                disk_type="hdd", is_os_disk=False)
    db.add(disk)
    db.commit()
    return disk


@pytest.mark.asyncio
async def test_declare_whole_disk_wipes_and_formats_part1(db_session, tmp_path, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID1")):
        rec = await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, label="Backup 1")

    assert rec["device_path"] == "/dev/disk/by-id/ata-X-part1"
    assert rec["slot_uuid"] is None
    assert rec["partition_number"] == 1
    assert rec["label"] == "Backup 1"
    assert rec["fs_uuid"] == "FSID1"
    parted = [c for c in cmds if c[0] == "parted"]
    assert any("mklabel" in c for c in parted)
    assert ("mkfs.ext4", "-F", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_declare_seeds_config_bundle_on_new_volume(db_session, tmp_path, monkeypatch):
    """A freshly declared volume is seeded with the configuration so it can
    rebuild the system even before any dataset backup runs."""
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))

    backup = MagicMock()
    backup.capture_config_bundle = AsyncMock(return_value={"id": "x"})

    async def fake_run_command(cmd, **kwargs):
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch.object(zfs_backup_manager, "backup", backup), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID1")):
        await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, label="Backup 1")

    backup.capture_config_bundle.assert_awaited_once()
    args = backup.capture_config_bundle.await_args.args
    assert args[0] is db_session
    assert str(args[1]) == str(tmp_path / "FSID1")


@pytest.mark.asyncio
async def test_declare_partition_formats_in_place_no_parted(db_session, tmp_path, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return ("", "", 0)

    async def fake_read_slot_uuids(paths):
        return {"/dev/sdb": {"partitions": [
            {"name": "sdb1", "partlabel": "nazman:slot-abc", "slot_uuid": "slot-abc", "size_bytes": 5 * 10**10},
        ]}}

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", side_effect=fake_read_slot_uuids), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID2")):
        rec = await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, slot_uuid="slot-abc", label="Media 2")

    assert rec["slot_uuid"] == "slot-abc"
    assert rec["partition_number"] == 1
    assert rec["device_path"] == "/dev/disk/by-id/ata-X-part1"
    assert rec["label"] == "Media 2"
    assert [c for c in cmds if c[0] == "parted"] == []
    assert ("mkfs.ext4", "-F", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]
    assert ("wipefs", "-a", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_declare_rejects_unknown_slot(db_session, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    async def fake_read_slot_uuids(paths):
        return {"/dev/sdb": {"partitions": []}}

    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):
        with pytest.raises(ValidationError):
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True, slot_uuid="missing-slot")

    assert db_session.query(BackupDisk).count() == 0


@pytest.mark.asyncio
async def test_declare_rejects_already_declared_disk(db_session, monkeypatch):
    disk = await _add_declare_disk(db_session)
    existing = BackupDisk(disk_id=disk.id, slot_uuid="slot-abc",
                          mount_point="/tmp/mnt", fs_uuid="EEX")
    db_session.add(existing)
    db_session.commit()
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"):
        with pytest.raises(ValidationError):
            await zfs_backup_manager.declare_backup_disk(db_session, disk.id, confirm=True)


@pytest.mark.asyncio
async def test_declare_rejects_without_confirm_and_os_disk(db_session, monkeypatch):
    osd = Disk(by_id="/dev/disk/by-id/nvme-OS", model="NVMe", serial="OS1",
               size_bytes=10**11, disk_type="nvme", is_os_disk=True)
    db_session.add(osd)
    db_session.commit()
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    with pytest.raises(ValidationError):
        await zfs_backup_manager.declare_backup_disk(db_session, osd.id, confirm=True)
    with pytest.raises(ValidationError):
        await zfs_backup_manager.declare_backup_disk(db_session, osd.id, confirm=False)
    assert db_session.query(BackupDisk).count() == 0


@pytest.mark.asyncio
async def test_declare_rejects_partition_pool_member(db_session, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    async def fake_read_slot_uuids(paths):
        return {"/dev/sdb": {"partitions": [
            {"name": "sdb1", "partlabel": "nazman:slot-abc", "slot_uuid": "slot-abc", "size_bytes": 5 * 10**10},
        ]}}

    members = {"/dev/disk/by-id/ata-X-part1": "poolA"}
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value=members)), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True, slot_uuid="slot-abc")
    assert "pool 'poolA'" in str(ei.value)


@pytest.mark.asyncio
async def test_declare_rejects_whole_disk_when_partition_is_pool_member(db_session):
    disk = await _add_declare_disk(db_session)
    members = {"/dev/disk/by-id/ata-X-part1": "poolA"}
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value=members)):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(db_session, disk.id, confirm=True)
    assert "pool 'poolA'" in str(ei.value)
    assert db_session.query(BackupDisk).count() == 0


@pytest.mark.asyncio
async def test_ensure_unused_rejects_md_member_and_mount():
    md_json = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk", "children": [
        {"name": "sdb1", "type": "part", "children": [{"name": "md127", "type": "md", "children": []}]}]}]})
    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=(md_json, "", 0)):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager._ensure_unused("/dev/sdb")
    assert "md127" in str(ei.value)

    mount_json = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk", "children": [
        {"name": "sdb1", "type": "part", "mountpoint": "/mnt/x"}]}]})
    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=(mount_json, "", 0)):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager._ensure_unused("/dev/sdb")
    assert "mounted at /mnt/x" in str(ei.value)


@pytest.mark.asyncio
async def test_ensure_unused_allows_free_device():
    free_json = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk", "children": [
        {"name": "sdb1", "type": "part"}]}]})
    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=(free_json, "", 0)):
        await zfs_backup_manager._ensure_unused("/dev/sdb")

    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=("", "no such device", 1)):
        await zfs_backup_manager._ensure_unused("/dev/sdb")


@pytest.mark.asyncio
async def test_declare_propagates_mkfs_failure(db_session):
    disk = await _add_declare_disk(db_session)
    db_session.commit()

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mkfs.ext4":
            raise CommandError(command="mkfs.ext4 -F /dev/disk/by-id/ata-X-part1",
                               returncode=1, stderr="device or resource busy")
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"):
        with pytest.raises(BackupError) as ei:
            await zfs_backup_manager.declare_backup_disk(db_session, disk.id, confirm=True)
    assert "device or resource busy" in str(ei.value)
    assert db_session.query(BackupDisk).count() == 0


def test_parse_mdadm_examine():
    stdout = """\
/dev/sdb1:
          Magic : a92b4efc
        Version : 1.2
          Name : pootlenaz:0
  Creation Time : ...
"""
    info = zfs_backup_manager._parse_mdadm_examine(stdout)
    assert info == {"name": "pootlenaz:0", "version": "1.2"}
    assert zfs_backup_manager._parse_mdadm_examine("") is None


EXAMINE_SB = """\
/dev/sdb1:
          Version : 1.2
          Name : pootlenaz:0
"""


def _cmd_log_fake(by_device=None, stop_rc=0):
    """Return a fake run_command that routes by command/device, logging calls.

    ``by_device`` maps device kernel path -> mdadm examine stdout (non-empty)
    to serve for ``mdadm --examine``; everything else succeeds.
    """
    by_device = by_device or {}
    calls = []

    async def fake_run_command(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "mdadm" and cmd[1] == "--examine":
            for dev, out in by_device.items():
                if cmd[2] == dev:
                    return (out, "", 0)
            return ("", "", 0)
        if cmd[0] == "mdadm" and cmd[1] == "--stop":
            return ("", "", stop_rc)
        if cmd[0] == "lsblk":
            return ('{"blockdevices": []}', "", 0)
        return ("", "", 0)

    return fake_run_command, calls


@pytest.mark.asyncio
async def test_api_raid_info_endpoint(client, db_session):
    disk = await _add_declare_disk(db_session)
    payload = {"device": "/dev/sdb", "md": [
        {"device": "/dev/sdb1", "name": "pootlenaz:0", "version": "1.2",
         "os_backing": False},
    ]}
    with patch.object(ZfsBackupManager, "get_raid_info",
                      new=AsyncMock(return_value=payload)):
        response = client.get(f"/api/backup-zfs/disks/{disk.id}/raid-info")
    assert response.status_code == 200, response.text
    assert response.json()["md"][0]["name"] == "pootlenaz:0"


@pytest.mark.asyncio
async def test_declare_requires_wipe_raid_when_superblock_present(db_session):
    disk = await _add_declare_disk(db_session)
    fake, _ = _cmd_log_fake({"/dev/sdb1": EXAMINE_SB})

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", new=AsyncMock(return_value={
             "/dev/sdb": {"partitions": [
                 {"name": "sdb1", "slot_uuid": "slot-abc", "size_bytes": 10},
             ]},
         })):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True)
    assert "software RAID metadata" in str(ei.value)


@pytest.mark.asyncio
async def test_declare_wipe_raid_stops_array_and_zeros_superblock(db_session, tmp_path):
    disk = await _add_declare_disk(db_session)

    def fake_lsblk(cmd, **kwargs):
        # lsblk on the whole disk shows the md array on sdb1.
        return ('{"blockdevices": [{"name": "sdb", "type": "disk", "children": ['
                '{"name": "sdb1", "type": "part", "children": ['
                '{"name": "md127", "type": "md", "children": []}]}]}]}', "", 0)

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mdadm" and cmd[1] == "--examine":
            if cmd[2] == "/dev/sdb1":
                return (EXAMINE_SB, "", 0)
            return ("", "", 0)
        if cmd[0] == "lsblk":
            return fake_lsblk(cmd)
        return ("", "", 0)

    cmds = []

    async def record_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return await fake_run_command(cmd)

    with patch("nazman.managers.zfs_backup_manager.run_command",
               side_effect=record_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", new=AsyncMock(return_value={
             "/dev/sdb": {"partitions": [
                 {"name": "sdb1", "partlabel": "nazman:slot-1", "slot_uuid": "slot-1",
                  "size_bytes": 10},
             ]},
         })), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID3")), \
         patch.object(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path)):
        rec = await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, wipe_raid=True)

    assert rec["fs_uuid"] == "FSID3"
    assert ("mdadm", "--stop", "/dev/md127") in [tuple(c) for c in cmds]
    assert ("mdadm", "--zero-superblock", "/dev/sdb1") in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_declare_wipe_raid_refuses_os_array(db_session):
    disk = await _add_declare_disk(db_session)

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mdadm" and cmd[1] == "--examine":
            if cmd[2] == "/dev/sdb1":
                return (EXAMINE_SB, "", 0)
            return ("", "", 0)
        if cmd[0] == "lsblk":
            return ('{"blockdevices": []}', "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", new=AsyncMock(return_value={
             "/dev/sdb": {"partitions": [
                 {"name": "sdb1", "slot_uuid": "slot-abc", "size_bytes": 10},
             ]},
         })), \
         patch("nazman.managers.zfs_backup_manager.os_reserved_partition_names",
               new=AsyncMock(return_value={"sdb1"})):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True, wipe_raid=True)
    assert "part of the OS" in str(ei.value)


@pytest.mark.asyncio
async def test_api_restore_run_ok(client, db_session, tmp_path):
    bd = BackupDisk(
        disk_id=999, mount_point=str(tmp_path), fs_uuid="AAA",
    )
    db_session.add(bd)
    db_session.flush()
    run = BackupRun(
        dataset_name="tank/media", backup_disk_id=bd.id,
        backup_type="full", stream_file="/tmp/nonexistent.zfs.gz",
        snapshot="tank/media@backup-x", status="success", size_bytes=10,
    )
    db_session.add(run)
    db_session.commit()

    with patch.object(ZfsBackupManager, "restore_dataset", new=AsyncMock(
        return_value={"dataset": "tank/media", "source": "/tmp/nonexistent.zfs.gz", "force": False}
    )):
        response = client.post(f"/api/backup-zfs/runs/{run.id}/restore", json={"target_dataset": "tank/media"})
    assert response.status_code == 200, response.text
    assert response.json()["dataset"] == "tank/media"


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_creates_zb_backup_tasks(db_session):
    from nazman.models.scheduler import ScheduledTask, TaskType

    bd = BackupDisk(
        disk_id=999, mount_point="/tmp/mnt", fs_uuid="AAA",
    )
    db_session.add(bd)
    db_session.flush()
    sched = BackupSchedule(
        dataset_name="tank/media", backup_disk_id=bd.id,
        full_cron="0 2 * * 0", incremental_cron="0 3 * * *", enabled=True,
    )
    db_session.add(sched)
    db_session.commit()

    await zfs_backup_manager.sync_scheduled_tasks(db_session)

    tasks = db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()
    assert len(tasks) == 2
    types = {t.config["type"] for t in tasks}
    assert types == {"full", "incremental"}


@pytest.mark.asyncio
async def test_api_list_disk_streams_and_restore_file(client, db_session, tmp_path):

    bd = BackupDisk(
        disk_id=998, mount_point=str(tmp_path), fs_uuid="BBB",
    )
    db_session.add(bd)
    db_session.commit()

    with patch.object(ZfsBackupManager, "list_stream_files", new=AsyncMock(
        return_value=[{"path": str(tmp_path / "x.zfs.gz"), "dataset": "tank", "size_bytes": 100}]
    )):
        resp = client.get(f"/api/backup-zfs/disks/{bd.id}/streams")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["dataset"] == "tank"

    with patch.object(ZfsBackupManager, "restore_dataset", new=AsyncMock(
        return_value={"dataset": "tank/media", "source": str(tmp_path / "x.zfs.gz"), "force": False}
    )):
        resp = client.post("/api/backup-zfs/restore-file",
                           json={"stream_file": str(tmp_path / "x.zfs.gz"), "target_dataset": "tank/media"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dataset"] == "tank/media"

    resp = client.post("/api/backup-zfs/restore-file", json={"stream_file": "", "target_dataset": "tank/media"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_probe_device_offline_when_byid_path_missing(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-GONE", model="HDD", serial="GONE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    state = await zfs_backup_manager._probe_device(bd)
    assert state == "offline"


@pytest.mark.asyncio
async def test_probe_device_unmounted_when_present_and_uuid_matches(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-HERE", model="HDD", serial="HERE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        state = await zfs_backup_manager._probe_device(bd)
    assert state == "unmounted"


@pytest.mark.asyncio
async def test_probe_device_mismatch_when_uuid_differs(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-SWAP", model="HDD", serial="SWAP1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="BBB")):
        state = await zfs_backup_manager._probe_device(bd)
    assert state == "mismatch"


@pytest.mark.asyncio
async def test_mount_backup_disk_refuses_offline(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-GONE", model="HDD", serial="GONE2",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(return_value="offline")), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=False)):
        with pytest.raises(BackupError, match="power-cycle"):
            await zfs_backup_manager.mount_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_mount_backup_disk_refuses_mismatch(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-SWAP", model="HDD", serial="SWAP2",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(return_value="mismatch")):
        with pytest.raises(BackupError, match="Filesystem changed"):
            await zfs_backup_manager.mount_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_mount_backup_disk_mounts_present_unmounted_device(db_session, tmp_path, monkeypatch):
    d = Disk(by_id="/dev/disk/by-id/ata-HERE", model="HDD", serial="HERE3",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point=str(tmp_path), fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    mounted = [False]
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[0] == "mount":
            mounted[0] = True
        return ("", "", 0)

    def fake_is_mount(self):
        return mounted[0] and str(self) == str(tmp_path)

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        rec = await zfs_backup_manager.mount_backup_disk(db_session, bd.id)

    assert rec["status"] == "mounted"
    assert ("mount", "/dev/disk/by-id/ata-HERE-part1", str(tmp_path)) in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_run_backup_offline_marks_run_failed(db_session):
    bd = BackupDisk(disk_id=999, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            return ("tank/media", "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=False)):
        run = await zfs_backup_manager.run_backup(
            db_session, dataset_name="tank/media", backup_disk_id=bd.id, backup_type="full"
        )

    assert run.status == "failed"
    assert "power-cycle" in (run.error or "").lower() or "not connected" in (run.error or "").lower()


@pytest.mark.asyncio
async def test_restore_dataset_mounts_owner_and_restores_idle(db_session, tmp_path):
    bd = BackupDisk(disk_id=999, mount_point=str(tmp_path), fs_uuid="BBB",
                    unmount_after_backup=True)
    db_session.add(bd)
    db_session.commit()

    fp = tmp_path / "data" / "tank" / "full-20260901-000000.zfs.gz"
    os.makedirs(fp.parent, exist_ok=True)
    fp.write_text("STREAMSIM")

    async def fake_pipeline(stages, **kwargs):
        assert stages[0][:2] == ["gunzip", "-c"]
        assert stages[1][:2] == ["zfs", "receive"]
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()) as mnt, \
         patch.object(zfs_backup_manager, "_restore_idle_state", new=AsyncMock()) as idle:
        res = await zfs_backup_manager.restore_dataset(db_session, str(fp), "tank/media")

    assert res["dataset"] == "tank/media"
    assert mnt.await_count == 1
    assert idle.await_count == 1


@pytest.mark.asyncio
async def test_restore_dataset_rejects_path_outside_backup_disks(db_session, tmp_path):
    bd = BackupDisk(disk_id=999, mount_point=str(tmp_path), fs_uuid="CCC")
    db_session.add(bd)
    db_session.commit()

    outside = tmp_path / "elsewhere" / "full-20260901-000000.zfs.gz"
    os.makedirs(outside.parent, exist_ok=True)
    outside.write_text("X")

    with patch("nazman.managers.zfs_backup_manager.run_pipeline", new=AsyncMock()):
        with pytest.raises(BackupError, match="under a registered backup disk"):
            await zfs_backup_manager.restore_dataset(db_session, str(outside), "tank/media")


@pytest.mark.asyncio
async def test_api_patch_backup_disk_toggle(client, db_session):
    bd = BackupDisk(disk_id=999, mount_point="/tmp/mnt", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    resp = client.patch(f"/api/backup-zfs/disks/{bd.id}", json={"unmount_after_backup": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["unmount_after_backup"] is False

    resp = client.patch(f"/api/backup-zfs/disks/{bd.id}", json={"unmount_after_backup": True})
    assert resp.status_code == 200
    assert resp.json()["unmount_after_backup"] is True


@pytest.mark.asyncio
async def test_api_mount_offline_returns_400(client, db_session):
    bd = BackupDisk(disk_id=999, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    with patch.object(ZfsBackupManager, "mount_backup_disk",
                      new=AsyncMock(side_effect=BackupError("Backup disk is offline"))):
        resp = client.post(f"/api/backup-zfs/disks/{bd.id}/mount")
    assert resp.status_code == 400
    assert "offline" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_task_names_include_disk_id(db_session):
    from nazman.models.scheduler import ScheduledTask, TaskType

    bd = BackupDisk(disk_id=997, mount_point="/tmp/mnt", fs_uuid="CCC")
    db_session.add(bd)
    db_session.flush()
    db_session.add(BackupSchedule(
        dataset_name="tank/media", backup_disk_id=bd.id,
        full_cron="0 2 * * 0", incremental_cron="0 3 * * *", enabled=True,
    ))
    db_session.commit()

    await zfs_backup_manager.sync_scheduled_tasks(db_session)

    names = {t.name for t in db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()}
    assert names == {f"zfs-full-tank/media-{bd.id}", f"zfs-incr-tank/media-{bd.id}"}
    task = db_session.query(ScheduledTask).filter(ScheduledTask.name == f"zfs-full-tank/media-{bd.id}").first()
    assert task.config["backup_disk_id"] == bd.id


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_cleans_legacy_unqualified_names(db_session):
    from nazman.models.scheduler import ScheduledTask, TaskType

    bd = BackupDisk(disk_id=996, mount_point="/tmp/mnt", fs_uuid="DDD")
    db_session.add(bd)
    db_session.flush()
    # Legacy task created before task names were disk-qualified.
    await scheduler_manager.create_task(
        db_session, name=f"zfs-full-tank/media-old", task_type=TaskType.ZFS_BACKUP,
        target="tank/media", schedule="0 2 * * 0",
        config={"dataset_name": "tank/media", "type": "full"},
    )
    db_session.commit()

    await zfs_backup_manager.sync_scheduled_tasks(db_session)

    remaining = db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_multiple_disk_schedules_per_dataset_via_api(client, db_session):
    from nazman.models.backup_zfs import BackupSchedule as Sched
    d1 = BackupDisk(disk_id=995, mount_point="/tmp/mnt1", fs_uuid="E1")
    d2 = BackupDisk(disk_id=994, mount_point="/tmp/mnt2", fs_uuid="E2")
    db_session.add_all([d1, d2])
    db_session.commit()

    base = {"dataset_name": "tank/media", "full_cron": "0 2 * * 0", "enabled": True}
    r1 = client.post("/api/backup-zfs/schedules", json={**base, "backup_disk_id": d1.id})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/backup-zfs/schedules", json={**base, "backup_disk_id": d2.id})
    assert r2.status_code == 200, r2.text
    assert db_session.query(Sched).filter(Sched.dataset_name == "tank/media").count() == 2

    # Re-saving the same (dataset, disk) pair updates in place, no duplicate row.
    r3 = client.post("/api/backup-zfs/schedules",
                     json={**base, "backup_disk_id": d1.id, "incremental_cron": "0 3 * * *"})
    assert r3.status_code == 200, r3.text
    assert db_session.query(Sched).filter(Sched.dataset_name == "tank/media").count() == 2
    dup = db_session.query(Sched).filter(
        Sched.dataset_name == "tank/media", Sched.backup_disk_id == d1.id).one()
    assert dup.incremental_cron == "0 3 * * *"


@pytest.mark.asyncio
async def test_delete_schedule_per_disk(client, db_session):
    d1 = BackupDisk(disk_id=993, mount_point="/tmp/mnt1", fs_uuid="F1")
    d2 = BackupDisk(disk_id=992, mount_point="/tmp/mnt2", fs_uuid="F2")
    db_session.add_all([d1, d2])
    db_session.commit()
    base = {"dataset_name": "tank/media", "enabled": True}
    client.post("/api/backup-zfs/schedules", json={**base, "backup_disk_id": d1.id})
    client.post("/api/backup-zfs/schedules", json={**base, "backup_disk_id": d2.id})

    resp = client.delete(f"/api/backup-zfs/schedules/tank/media?backup_disk_id={d1.id}")
    assert resp.status_code == 200, resp.text

    remaining = db_session.query(BackupSchedule).filter(
        BackupSchedule.dataset_name == "tank/media").all()
    assert [s.backup_disk_id for s in remaining] == [d2.id]


@pytest.mark.asyncio
async def test_backup_schedule_composite_unique_constraint(db_session):
    from sqlalchemy.exc import IntegrityError
    d1 = BackupDisk(disk_id=991, mount_point="/tmp/mnt1", fs_uuid="G1")
    db_session.add(d1)
    db_session.flush()
    first = BackupSchedule(dataset_name="tank/media", backup_disk_id=d1.id, enabled=True)
    db_session.add(first)
    db_session.commit()
    second = BackupSchedule(dataset_name="tank/media", backup_disk_id=d1.id, enabled=True)
    db_session.add(second)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.asyncio
async def test_list_backupable_datasets_shapes_per_disk_schedules(client, db_session):
    from unittest.mock import patch as _patch

    d1 = BackupDisk(disk_id=990, label="Backup A1", mount_point="/tmp/mnt1", fs_uuid="H1")
    d2 = BackupDisk(disk_id=989, label="Backup A2", mount_point="/tmp/mnt2", fs_uuid="H2")
    db_session.add_all([d1, d2])
    db_session.commit()
    db_session.add_all([
        BackupSchedule(dataset_name="tank/media", backup_disk_id=d1.id,
                       full_cron="0 2 * * 0", enabled=True),
        BackupSchedule(dataset_name="tank/media", backup_disk_id=d2.id,
                       incremental_cron="0 3 * * *", enabled=True),
        BackupRun(dataset_name="tank/media", backup_disk_id=d1.id, backup_type="full",
                  stream_file="f", snapshot="s", size_bytes=0, changed_bytes=0,
                  status="success"),
        BackupRun(dataset_name="tank/media", backup_disk_id=d2.id, backup_type="full",
                  stream_file="f", snapshot="s", size_bytes=0, changed_bytes=0,
                  status="failed", error="boom"),
    ])
    db_session.commit()

    with _patch("nazman.utils.zfs_query.run_zpool", new=AsyncMock(
             return_value=("tank\n", "", 0))), \
         _patch("nazman.utils.zfs_query.run_zfs", new=AsyncMock(
             return_value=("tank\ntank/media\n", "", 0))):
        resp = client.get("/api/backup-zfs/datasets")
    assert resp.status_code == 200, resp.text
    data = resp.json()[0]
    assert len(data["schedules"]) == 2
    by_disk = {s["backup_disk_id"]: s for s in data["schedules"]}
    assert by_disk[d1.id]["label"] == "Backup A1"
    assert by_disk[d1.id]["last_status"] == "success"
    assert by_disk[d2.id]["label"] == "Backup A2"
    assert by_disk[d2.id]["last_status"] == "failed"
    assert by_disk[d2.id]["last_type"] == "full"
    # Dataset-level last backup reflects most recent run across all disks.
    assert data["last_status"] == "failed"


@pytest.mark.asyncio
async def test_wake_backup_disk_toggles_bridge_authorized(db_session, tmp_path):
    d = Disk(by_id="/dev/disk/by-id/ata-AWAKE", model="HDD", serial="AWAKE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    authorized = bridge / "authorized"
    authorized.write_text("0")

    real_exists = os.path.exists
    dev_path = zfs_backup_manager._dev_path(bd)

    def fake_exists(p):
        if str(p) == dev_path:
            return authorized.read_text() == "1"
        return real_exists(p)

    with patch("nazman.managers.zfs_backup_manager.os.path.exists", side_effect=fake_exists), \
         patch.object(zfs_backup_manager, "_usb_storage_bridges", return_value=[bridge]), \
         patch("nazman.managers.zfs_backup_manager.asyncio.sleep", new=AsyncMock()):
        ok = await zfs_backup_manager._wake_backup_disk(bd)

    assert ok is True
    assert authorized.read_text() == "1"


@pytest.mark.asyncio
async def test_wake_backup_disk_requires_power_cycle_when_bridge_gone(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-BRIDGELESS", model="HDD", serial="BRIDGE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    with patch("os.path.exists", return_value=False), \
         patch.object(zfs_backup_manager, "_usb_storage_bridges", return_value=[]):
        ok = await zfs_backup_manager._wake_backup_disk(bd)

    assert ok is False


@pytest.mark.asyncio
async def test_wake_backup_disk_public_raises_on_failure(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-WGONE", model="HDD", serial="WGONE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(return_value="offline")), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=False)):
        with pytest.raises(BackupError, match="power-cycle"):
            await zfs_backup_manager.wake_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_mount_backup_disk_wakes_offline_disk_and_mounts(db_session, tmp_path, monkeypatch):
    d = Disk(by_id="/dev/disk/by-id/ata-HERE", model="HDD", serial="HERE4",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point=str(tmp_path), fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    probes = ["offline", "unmounted", "mounted"]

    async def fake_probe(rec):
        return probes.pop(0)

    mounted = [False]
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[0] == "mount":
            mounted[0] = True
        return ("", "", 0)

    def fake_is_mount(self):
        return mounted[0] and str(self) == str(tmp_path)

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    wake_mock = AsyncMock(return_value=True)

    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(side_effect=fake_probe)), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=wake_mock), \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        rec = await zfs_backup_manager.mount_backup_disk(db_session, bd.id)
        wake_mock.assert_awaited_once()
    assert rec["status"] == "mounted"
    assert ("mount", "/dev/disk/by-id/ata-HERE-part1", str(tmp_path)) in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_mount_backup_disk_still_offline_after_wake_raises(db_session, tmp_path, monkeypatch):
    d = Disk(by_id="/dev/disk/by-id/ata-NOPE", model="HDD", serial="NOPE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point=str(tmp_path), fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    probes = ["offline", "offline"]

    async def fake_probe(rec):
        return probes.pop(0)

    monkeypatch.setattr(Path, "is_mount", lambda self: False)

    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(side_effect=fake_probe)), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=True)):
        with pytest.raises(BackupError, match="power-cycle"):
            await zfs_backup_manager.mount_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_api_wake_backup_disk(client, db_session):
    bd = BackupDisk(disk_id=1, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    bd_dict = {"id": bd.id, "disk_id": 1, "slot_uuid": None, "partition_number": 1,
               "device_path": None, "label": None, "fs_type": "ext4",
               "mount_point": "/tmp/w", "fs_uuid": "AAA", "unmount_after_backup": True,
               "status": "offline", "total_bytes": 0, "free_bytes": 0}

    with patch.object(ZfsBackupManager, "wake_backup_disk", new=AsyncMock(return_value=bd_dict)):
        resp = client.post(f"/api/backup-zfs/disks/{bd.id}/wake")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "offline"

    with patch.object(ZfsBackupManager, "wake_backup_disk",
                      new=AsyncMock(side_effect=BackupError("power-cycle their enclosure"))):
        resp = client.post(f"/api/backup-zfs/disks/{bd.id}/wake")
    assert resp.status_code == 400
    assert "power-cycle" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dev_path_derived_from_by_id_and_partition_number(db_session):
    d1 = Disk(by_id="/dev/disk/by-id/ata-DERIVED", model="HDD", serial="DERIVED1",
              size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    d2 = Disk(by_id="/dev/disk/by-id/ata-DERIVED2", model="HDD", serial="DERIVED2",
              size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    db_session.add_all([d1, d2])
    db_session.flush()
    whole = BackupDisk(mount_point="/tmp/p", fs_uuid="W", partition_number=1)
    part2 = BackupDisk(mount_point="/tmp/p2", fs_uuid="P", partition_number=2)
    whole.disk_id = d1.id
    part2.disk_id = d2.id
    db_session.add_all([whole, part2])
    db_session.commit()
    assert zfs_backup_manager._dev_path(whole) == "/dev/disk/by-id/ata-DERIVED-part1"
    assert zfs_backup_manager._dev_path(part2) == "/dev/disk/by-id/ata-DERIVED2-part2"
    none_disk = BackupDisk(disk_id=12345, mount_point="/tmp/none", fs_uuid="N")
    db_session.add(none_disk)
    db_session.commit()
    assert zfs_backup_manager._dev_path(none_disk) is None
    assert zfs_backup_manager._partition_number("/dev/disk/by-id/ata-X-part1") == 1
    assert zfs_backup_manager._partition_number("/dev/disk/by-id/ata-X-part7") == 7
    assert zfs_backup_manager._partition_number("/dev/sdb") == 1


def test_backup_disk_requires_unique_disk_and_fs_uuid(db_session):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db_session.add_all([
            BackupDisk(disk_id=1, mount_point="/tmp/a", fs_uuid="UU-1"),
            BackupDisk(disk_id=1, mount_point="/tmp/b", fs_uuid="UU-2"),
        ])
        db_session.commit()
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.add_all([
            BackupDisk(disk_id=2, mount_point="/tmp/a", fs_uuid="UU-3"),
            BackupDisk(disk_id=3, mount_point="/tmp/b", fs_uuid="UU-3"),
        ])
        db_session.commit()


@pytest.mark.asyncio
async def test_deregister_backup_disk_removes_runs_and_schedules(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-DEREG", model="HDD", serial="DEREG1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    db_session.add(d)
    db_session.flush()
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/dreg", fs_uuid="DRG")
    db_session.add(bd)
    db_session.flush()
    sched = BackupSchedule(dataset_name="tank/media", backup_disk_id=bd.id,
                           full_cron="0 2 * * *", enabled=True)
    run = BackupRun(dataset_name="tank/media", backup_disk_id=bd.id,
                    backup_type="full", stream_file="/tmp/dreg/full.zfs.gz",
                    snapshot="tank/media@backup-x", status="success")
    db_session.add_all([sched, run])
    db_session.commit()

    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=("", "", 0)):
        await zfs_backup_manager.deregister_backup_disk(db_session, bd.id)

    assert db_session.query(BackupDisk).count() == 0
    assert db_session.query(BackupSchedule).count() == 0
    assert db_session.query(BackupRun).count() == 0


@pytest.mark.asyncio
async def test_api_start_run_backup_returns_202(client, db_session):
    """POST /runs registers the run row, returns 202, and spawns the worker."""
    bd = BackupDisk(disk_id=999, mount_point="/tmp/mnt", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    # Real start_run_backup: it persists the run row and spawns the worker
    # task; only the spawn is mocked so no backup actually executes.
    with patch("nazman.utils.zfs_query.run_zfs",
               AsyncMock(return_value=("tank/media", "", 0))), \
         patch("nazman.managers.zfs_backup_manager.asyncio.create_task") as ct:
        response = client.post("/api/backup-zfs/runs", json={
            "dataset_name": "tank/media", "backup_disk_id": bd.id, "backup_type": "full",
        })
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "running"
    assert data["dataset_name"] == "tank/media"
    ct.assert_called_once()


@pytest.mark.asyncio
async def test_run_backup_incremental_uses_prior_anchor(db_session, tmp_path, monkeypatch):
    """Incremental must send -i <prior-anchor> <new-snapshot>, never -i <new> <new>.

    Regresses the ordering bug where the anchor was found AFTER the new
    snapshot was created, so the anchor resolved to the snapshot itself and
    `zfs send -R -i <snap> <snap>` failed ("incremental source ... is not
    earlier than it").
    """
    bd = BackupDisk(
        disk_id=999, mount_point=str(tmp_path), fs_uuid="AAA",
        unmount_after_backup=True,
    )
    db_session.add(bd)
    db_session.commit()

    anchor = "tank/media@backup-20260901-120000"
    snaps = {anchor}
    mounted = [True]
    sends = []

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "snapshot":
            snaps.add(cmd[2])
            return ("", "", 0)
        if cmd and cmd[0] == "destroy":
            return ("", "", 0)
        if cmd and cmd[0] == "get":
            return ("123456", "", 0)
        if cmd and cmd[0] == "diff":
            # Report changes so the run is not skipped.
            return ("M\tsome/file\n", "", 0)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("\n".join(sorted(snaps)), "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        sends.append(list(stages[0]))
        os.makedirs(os.path.dirname(stdout_path), exist_ok=True)
        with open(stdout_path, "w") as f:
            f.write("INCRSTREAM")
        return ("", "", 0)

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mount":
            mounted[0] = True
        if cmd[0] == "umount":
            mounted[0] = False
        return ("", "", 0)

    def fake_is_mount(self):
        return mounted[0] and str(self) == str(tmp_path)

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        run = await zfs_backup_manager.run_backup(
            db_session, dataset_name="tank/media", backup_disk_id=bd.id,
            backup_type="incremental",
        )

    assert run.status == "success"
    assert run.backup_type == "incremental"
    assert run.base_snapshot == anchor
    assert len(sends) == 1
    send = sends[0]
    assert send[:3] == ["zfs", "send", "-R"]
    # -i must reference the pre-existing anchor, not the freshly created one.
    base_arg = send[send.index("-i") + 1]
    assert base_arg == anchor
    assert send[-1] != anchor
