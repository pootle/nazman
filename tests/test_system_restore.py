from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nazman.services.system_restore import SystemRestoreService
from nazman.utils import backup_manifest as bm
from nazman.utils.exceptions import BackupError, ValidationError
from nazman.wiring import get_system_restore_service
from tests.conftest import override_manager


def _write_volume(root: Path, media_uuid: str = "AAA") -> dict:
    manifest = bm.new_manifest(media={"fs_uuid": media_uuid, "label": "vol1"})
    bm.merge_pools(manifest, [{
        "name": "tank", "ashift": 12,
        "vdevs": [{
            "role": "data", "topology": "mirror", "ashift": 12,
            "devices": [
                {"by_id": "/dev/disk/by-id/ata-A", "serial": "SA", "size_bytes": 100},
                {"by_id": "/dev/disk/by-id/ata-B", "serial": "SB", "size_bytes": 100},
            ],
        }],
    }])
    bm.upsert_dataset_backup(
        manifest,
        {"name": "tank/media", "pool": "tank", "properties": {}, "mountpoint": "/tank/media"},
        {
            "type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
            "snapshot": "tank/media@backup-1", "base_snapshot": None, "full_anchor": None,
            "size_bytes": 10, "sha256": "x", "created_at": "2026-01-01T00:00:00",
            "media_fs_uuid": media_uuid, "media_label": "vol1",
        },
    )
    bm.save_manifest(root, manifest)
    stream = root / "data/tank/media/full-1.zfs.gz"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_bytes(b"x")
    return manifest


def _candidate(fs_uuid="AAA"):
    return {
        "device": "/dev/sdb1", "device_name": "sdb1", "by_id": "/dev/disk/by-id/ata-D-part1",
        "base_by_id": "/dev/disk/by-id/ata-D", "base_name": "sdb", "partition_number": 1,
        "fstype": "ext4", "fs_uuid": fs_uuid, "label": "vol1", "partlabel": None,
        "size_bytes": 100,
    }


@pytest.mark.asyncio
async def test_discover_backup_sets_reads_manifest(db_session, tmp_path):
    volume = tmp_path / "vol"
    volume.mkdir()
    _write_volume(volume)

    service = SystemRestoreService()
    service.list_candidates = AsyncMock(return_value=[_candidate()])
    service._mount_readonly = AsyncMock(return_value=volume)
    service._unmount = AsyncMock()

    sets = await service.discover_backup_sets(db_session)
    assert len(sets) == 1
    assert sets[0]["set_id"] == "AAA"
    assert sets[0]["pool_names"] == ["tank"]
    assert sets[0]["dataset_count"] == 1


@pytest.mark.asyncio
async def test_plan_pool_mapping_matches_by_id_then_serial(db_session):
    service = SystemRestoreService()
    manifest = {
        "pools": [{
            "name": "tank", "ashift": 12,
            "vdevs": [{
                "role": "data", "topology": "mirror", "ashift": 12,
                "devices": [
                    {"by_id": "/dev/disk/by-id/ata-A", "serial": "SA", "size_bytes": 100},
                    {"by_id": "/dev/disk/by-id/ata-C", "serial": "SC", "size_bytes": 100},
                ],
            }],
        }],
    }
    service._manifest_for = AsyncMock(return_value=manifest)
    service._attached_disks = AsyncMock(return_value=[
        {"disk_id": 1, "by_id": "/dev/disk/by-id/ata-A", "serial": "SA", "size_bytes": 100,
         "model": "A", "device_path": "/dev/sda", "present": True, "is_os_disk": False},
        {"disk_id": 2, "by_id": "/dev/disk/by-id/ata-X", "serial": "SC", "size_bytes": 200,
         "model": "X", "device_path": "/dev/sdc", "present": True, "is_os_disk": False},
        {"disk_id": 3, "by_id": "/dev/disk/by-id/ata-Z", "serial": "SZ", "size_bytes": 100,
         "model": "Z", "device_path": "/dev/sdz", "present": True, "is_os_disk": False},
    ])

    plan = await service.plan_pool_mapping(db_session, "AAA", "tank")
    devices = plan["vdevs"][0]["devices"]
    assert devices[0]["matched_disk_id"] == 1 and devices[0]["match"] == "by_id"
    assert devices[1]["matched_disk_id"] == 2 and devices[1]["match"] == "serial"
    assert [d["disk_id"] for d in plan["available_disks"]] == [3]


@pytest.mark.asyncio
async def test_restore_plan_defaults_enabled_with_matching_pool(db_session):
    zfs = MagicMock()
    zfs.list_pool_names = AsyncMock(return_value=["tank"])
    service = SystemRestoreService(zfs=zfs)
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{
            "name": "tank/media", "pool": "tank",
            "backups": [{
                "type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
                "created_at": "2026-01-01T00:00:00", "media_fs_uuid": "AAA",
            }],
        }],
    })

    plan = await service.restore_plan(db_session, "AAA")
    assert plan[0]["source_dataset"] == "tank/media"
    assert plan[0]["leaf"] == "media"
    assert plan[0]["suggested_pool"] == "tank"
    assert plan[0]["enabled"] is True


@pytest.mark.asyncio
async def test_required_media_marks_connected(db_session):
    service = SystemRestoreService()
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{
            "name": "tank/media",
            "backups": [{
                "type": "full", "stream_file": "a", "created_at": "2026-01-01T00:00:00",
                "media_fs_uuid": "AAA", "media_label": "vol1",
            }],
        }],
    })
    service.list_candidates = AsyncMock(return_value=[_candidate("AAA")])

    media = await service.required_media(db_session, "AAA")
    assert media[0]["media_fs_uuid"] == "AAA"
    assert media[0]["connected"] is True
    assert media[0]["datasets"] == ["tank/media"]


@pytest.mark.asyncio
async def test_restore_datasets_replays_full_then_incrementals(db_session, tmp_path):
    volume = tmp_path / "vol"
    media_dir = volume / "data/tank/media"
    media_dir.mkdir(parents=True)
    for name in ("full-1.zfs.gz", "incr-2.zfs.gz", "incr-3.zfs.gz"):
        (media_dir / name).write_bytes(b"x")

    chain = [
        {"type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
         "created_at": "2026-01-01T00:00:00", "media_fs_uuid": "AAA"},
        {"type": "incremental", "stream_file": "data/tank/media/incr-2.zfs.gz",
         "created_at": "2026-01-02T00:00:00", "media_fs_uuid": "AAA"},
        {"type": "incremental", "stream_file": "data/tank/media/incr-3.zfs.gz",
         "created_at": "2026-01-03T00:00:00", "media_fs_uuid": "AAA"},
    ]
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{"name": "tank/media", "pool": "tank", "backups": chain}],
        "_volume_root": str(volume),
        "_candidate": _candidate(),
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service.zfs_backup.receive_stream = AsyncMock(return_value={"ok": True})
    service._mount_for_set = AsyncMock(return_value=(_candidate(), volume))
    service._unmount = AsyncMock()

    result = await service.restore_datasets(
        db_session, "AAA",
        [{"source_dataset": "tank/media", "target_pool": "newtank", "enabled": True}],
    )

    calls = service.zfs_backup.receive_stream.await_args_list
    assert [Path(c.args[0]).name for c in calls] == [
        "full-1.zfs.gz", "incr-2.zfs.gz", "incr-3.zfs.gz",
    ]
    assert [c.args[1] for c in calls] == ["newtank/media"] * 3
    assert result["results"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_restore_datasets_media_filter_skips_other_media(db_session, tmp_path):
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{
            "name": "tank/media", "pool": "tank",
            "backups": [{
                "type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
                "created_at": "2026-01-01T00:00:00", "media_fs_uuid": "AAA",
            }],
        }],
        "_volume_root": str(tmp_path),
        "_candidate": _candidate(),
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service.zfs_backup.receive_stream = AsyncMock()
    service._mount_for_set = AsyncMock(return_value=(_candidate(), tmp_path))
    service._unmount = AsyncMock()

    result = await service.restore_datasets(
        db_session, "AAA",
        [{"source_dataset": "tank/media", "target_pool": "tank", "enabled": True}],
        media_fs_uuid="BBB",
    )
    assert result["results"] == []
    service.zfs_backup.receive_stream.assert_not_called()


@pytest.mark.asyncio
async def test_create_pool_from_backup_rejects_existing(db_session):
    zfs = MagicMock()
    zfs.list_pool_names = AsyncMock(return_value=["tank"])
    service = SystemRestoreService(zfs=zfs)
    with pytest.raises(ValidationError):
        await service.create_pool_from_backup(
            db_session, "AAA", "tank",
            [{"role": "data", "topology": "stripe", "devices": [{"disk_id": 1}]}],
        )


@pytest.mark.asyncio
async def test_create_pool_from_backup_calls_zfs(db_session):
    zfs = MagicMock()
    zfs.list_pool_names = AsyncMock(return_value=[])
    zfs.create_pool = AsyncMock(return_value={"name": "tank"})
    service = SystemRestoreService(zfs=zfs)
    vdevs = [{"role": "data", "topology": "stripe", "devices": [{"disk_id": 1, "slot_uuid": None}]}]
    out = await service.create_pool_from_backup(db_session, "AAA", "tank", vdevs)
    assert out["name"] == "tank"
    zfs.create_pool.assert_awaited_once_with(db_session, "tank", vdevs)


@pytest.mark.asyncio
async def test_adopt_media_requires_fs_uuid(db_session):
    service = SystemRestoreService()
    service._find_candidate = AsyncMock(return_value={"device": "/dev/sdb1", "fs_uuid": None})
    with pytest.raises(ValidationError):
        await service.adopt_media(db_session, "dev:sdb1")


@pytest.mark.asyncio
async def test_api_list_backup_sets(client):
    with override_manager(get_system_restore_service) as mock:
        mock.discover_backup_sets = AsyncMock(return_value=[
            {"set_id": "AAA", "pool_names": ["tank"]},
        ])
        response = client.get("/api/system-restore/sets")
    assert response.status_code == 200
    assert response.json()[0]["set_id"] == "AAA"


@pytest.mark.asyncio
async def test_api_restore_datasets_maps_error_to_400(client):
    with override_manager(get_system_restore_service) as mock:
        mock.restore_datasets = AsyncMock(side_effect=BackupError("boom"))
        response = client.post(
            "/api/system-restore/sets/AAA/datasets/restore",
            json={"selections": []},
        )
    assert response.status_code == 400
    assert "boom" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_dataset_restore_plan(client):
    with override_manager(get_system_restore_service) as mock:
        mock.restore_plan = AsyncMock(return_value=[
            {"source_dataset": "tank/media", "enabled": True},
        ])
        response = client.get("/api/system-restore/sets/AAA/datasets/plan")
    assert response.status_code == 200
    assert response.json()[0]["source_dataset"] == "tank/media"
