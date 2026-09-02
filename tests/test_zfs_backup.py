import json
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from nazman.models.disk import Disk
from nazman.models.pool import Pool
from nazman.models.backup_zfs import BackupDisk, BackupRun, BackupSchedule
from nazman.managers.zfs_backup_manager import zfs_backup_manager


def _mk_pool(db_session, name):
    pool = Pool(name=name)
    db_session.add(pool)
    db_session.flush()
    return pool


@pytest.mark.asyncio
async def test_run_backup_full_writes_successful_run(db_session, tmp_path, monkeypatch):
    bd = BackupDisk(
        disk_id=999, device_path="/dev/disk/by-id/usb-X-part1",
        mount_point=str(tmp_path), fs_uuid="AAA", status="mounted",
        total_bytes=10**18, free_bytes=10**15,
    )
    db_session.add(bd)
    db_session.commit()

    snap_name = "tank/media@backup-20260901-000000"
    stream_file = str(tmp_path / "data/tank/media/full-20260901-000000.zfs.gz")
    os.makedirs(os.path.dirname(stream_file), exist_ok=True)

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

    async def fake_pipeline(cmd, timeout=3600, check=True):
        # cmd looks like: zfs send ... > /path/to/full-<ts>.zfs.gz
        out = cmd.rsplit(">", 1)[-1].strip().strip("'\"")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write("STREAMSIM")
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs) as rzfs, \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline) as rpipe:
        run = await zfs_backup_manager.run_backup(
            db_session, dataset_name="tank/media", backup_disk_id=bd.id, backup_type="full"
        )

    assert run.status == "success"
    assert run.backup_type == "full"
    assert run.snapshot.startswith("tank/media@backup-")
    assert run.stream_file.endswith(".zfs.gz")
    assert run.size_bytes == len("STREAMSIM")
    assert run.dataset_name == "tank/media"


@pytest.mark.asyncio
async def test_run_backup_capacity_insufficient_aborts(db_session, tmp_path):
    bd = BackupDisk(
        disk_id=999, device_path="/dev/disk/by-id/usb-X-part1",
        mount_point=str(tmp_path), fs_uuid="AAA", status="mounted",
        total_bytes=1024, free_bytes=100,  # too small for margin on used=123456
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

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
         patch.object(zfs_backup_manager, "mount_backup_disk", new=AsyncMock()), \
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
        disk_id=999, device_path="/dev/disk/by-id/usb-X-part1",
        mount_point="/tmp/mnt", fs_uuid="AAA", status="mounted",
        total_bytes=10**18, free_bytes=10**15,
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

    with patch("nazman.api.zfs_backup.zfs_backup_manager") as mock:
        mock.run_backup = AsyncMock(return_value=run)
        response = client.post("/api/backup-zfs/runs", json={
            "dataset_name": "tank/media", "backup_disk_id": bd.id, "backup_type": "full",
        })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dataset_name"] == "tank/media"
    assert data["status"] == "success"


@pytest.mark.asyncio
async def test_api_backup_disk_candidates(client, db_session):
    disk = Disk(
        by_id="/dev/disk/by-id/usb-CAND", model="USB", serial="CAND1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(disk)
    db_session.commit()

    with patch("nazman.managers.disk_manager.disk_manager.sync_disks_to_database", AsyncMock(return_value=[disk])):
        response = client.get("/api/backup-zfs/disks/candidates")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == disk.id


@pytest.mark.asyncio
async def test_api_candidates_excludes_os_disk(client, db_session):
    osd = Disk(
        by_id="/dev/disk/by-id/nvme-OS", model="NVMe", serial="OS1",
        size_bytes=10**11, disk_type="nvme", is_os_disk=True,
    )
    db_session.add(osd)
    db_session.commit()

    with patch("nazman.managers.disk_manager.disk_manager.sync_disks_to_database", AsyncMock(return_value=[osd])):
        response = client.get("/api/backup-zfs/disks/candidates")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_restore_run_ok(client, db_session, tmp_path):
    bd = BackupDisk(
        disk_id=999, device_path="/dev/disk/by-id/usb-X-part1",
        mount_point=str(tmp_path), fs_uuid="AAA", status="mounted",
        total_bytes=10**18, free_bytes=10**15,
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

    with patch.object(zfs_backup_manager, "restore_dataset", new=AsyncMock(
        return_value={"dataset": "tank/media", "source": "/tmp/nonexistent.zfs.gz"}
    )):
        response = client.post(f"/api/backup-zfs/runs/{run.id}/restore", json={"dataset_name": "tank/media"})
    assert response.status_code == 200, response.text
    assert response.json()["dataset"] == "tank/media"


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_creates_zb_backup_tasks(db_session):
    from nazman.models.scheduler import ScheduledTask, TaskType
    from nazman.managers.zfs_backup_manager import zfs_backup_manager

    bd = BackupDisk(
        disk_id=999, device_path="/dev/disk/by-id/usb-X-part1",
        mount_point="/tmp/mnt", fs_uuid="AAA", status="mounted",
        total_bytes=10**18, free_bytes=10**15,
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
    from nazman.managers.zfs_backup_manager import zfs_backup_manager

    bd = BackupDisk(
        disk_id=998, device_path="/dev/disk/by-id/usb-Y-part1",
        mount_point=str(tmp_path), fs_uuid="BBB", status="mounted",
        total_bytes=10**18, free_bytes=10**15,
    )
    db_session.add(bd)
    db_session.commit()

    with patch.object(zfs_backup_manager, "list_stream_files", new=AsyncMock(
        return_value=[{"path": str(tmp_path / "x.zfs.gz"), "dataset": "tank", "size_bytes": 100}]
    )):
        resp = client.get(f"/api/backup-zfs/disks/{bd.id}/streams")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["dataset"] == "tank"

    with patch.object(zfs_backup_manager, "restore_dataset", new=AsyncMock(
        return_value={"dataset": "tank/media", "source": str(tmp_path / "x.zfs.gz")}
    )):
        resp = client.post("/api/backup-zfs/restore-file",
                           json={"stream_file": str(tmp_path / "x.zfs.gz"), "dataset_name": "tank/media"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dataset"] == "tank/media"

    resp = client.post("/api/backup-zfs/restore-file", json={"stream_file": "", "dataset_name": "tank/media"})
    assert resp.status_code == 400
