from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nazman.services.system_restore import SystemRestoreService
from nazman.utils import backup_manifest as bm
from nazman.utils.exceptions import BackupError, ConflictError, ValidationError
from nazman.wiring import get_system_restore_service
from tests.conftest import override_manager


def _write_volume(root: Path, media_uuid: str = "AAA", set_id=None,
                  stream: str = "data/tank/media/full-1.zfs.gz", group=None,
                  updated_at=None) -> dict:
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
    run = {
        "type": "full", "stream_file": stream,
        "snapshot": "tank/media@backup-1", "base_snapshot": None, "full_anchor": None,
        "size_bytes": 10, "sha256": "x", "created_at": "2026-01-01T00:00:00",
        "media_fs_uuid": media_uuid, "media_label": "vol1",
    }
    if set_id is not None:
        run["set_id"] = set_id
    bm.upsert_dataset_backup(
        manifest,
        {"name": "tank/media", "pool": "tank", "properties": {}, "mountpoint": "/tank/media"},
        run,
    )
    if group is not None:
        manifest["group"] = group
    bm.save_manifest(root, manifest)
    if updated_at is not None:
        manifest["updated_at"] = updated_at
        bm.atomic_write_json(Path(root) / bm.MANIFEST_NAME, manifest)
    stream_path = root / stream
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    stream_path.write_bytes(b"x")
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
async def test_plan_pool_mapping_matches_existing_partitions(db_session):
    service = SystemRestoreService()
    manifest = {
        "pools": [{
            "name": "tank", "ashift": 12,
            "vdevs": [{
                "role": "data", "topology": "mirror", "ashift": 12,
                "devices": [
                    {"by_id": "/dev/disk/by-id/ata-A", "serial": "SA", "size_bytes": 100,
                     "slot_uuid": "SLOT1", "partition_number": 1, "partition_size_bytes": 40_000_000},
                    {"by_id": "/dev/disk/by-id/ata-B", "serial": "SB", "size_bytes": 100,
                     "slot_uuid": "SLOT2", "partition_number": 1, "partition_size_bytes": 999_000_000},
                    {"by_id": "/dev/disk/by-id/ata-C", "serial": "SC", "size_bytes": 100,
                     "slot_uuid": "MISSING", "partition_number": 1},
                ],
            }],
        }],
    }
    service._manifest_for = AsyncMock(return_value=manifest)
    service._attached_disks = AsyncMock(return_value=[
        {"disk_id": 1, "by_id": "/dev/disk/by-id/ata-A", "serial": "SA", "size_bytes": 100,
         "model": "A", "device_path": "/dev/sda", "present": True, "is_os_disk": False,
         "partitions": [{"number": 1, "slot_uuid": "SLOT1",
                         "device_path": "/dev/disk/by-id/ata-A-part1", "size_bytes": 50_000_000}]},
        {"disk_id": 2, "by_id": "/dev/disk/by-id/ata-B", "serial": "SB", "size_bytes": 200,
         "model": "B", "device_path": "/dev/sdb", "present": True, "is_os_disk": False,
         "partitions": [{"number": 1, "slot_uuid": "SLOT2",
                         "device_path": "/dev/disk/by-id/ata-B-part1", "size_bytes": 500_000_000}]},
        {"disk_id": 3, "by_id": "/dev/disk/by-id/ata-C", "serial": "SC", "size_bytes": 200,
         "model": "C", "device_path": "/dev/sdc", "present": True, "is_os_disk": False,
         "partitions": [{"number": 1, "slot_uuid": "OTHER",
                         "device_path": "/dev/disk/by-id/ata-C-part1", "size_bytes": 500_000_000}]},
    ])

    plan = await service.plan_pool_mapping(db_session, "AAA", "tank")
    devices = plan["vdevs"][0]["devices"]
    assert devices[0]["matched_disk_id"] == 1
    assert devices[0]["matched_partition"]["slot_uuid"] == "SLOT1"
    assert devices[0]["size_ok"] is True
    assert devices[1]["matched_disk_id"] == 2
    assert devices[1]["matched_partition"]["slot_uuid"] == "SLOT2"
    assert devices[1]["size_ok"] is False
    assert devices[2]["matched_disk_id"] == 3
    assert devices[2]["matched_partition"] is None
    assert [d["disk_id"] for d in plan["attached_disks"]] == [1, 2, 3]
    assert plan["available_disks"] == []


@pytest.mark.asyncio
async def test_attached_disks_excludes_media_and_pool_members(db_session, monkeypatch):
    import nazman.services.system_restore as sr
    from nazman.models.disk import Disk

    d_media = Disk(by_id="/dev/disk/by-id/ata-MEDIA", serial="SM", size_bytes=100, disk_type="hdd", is_os_disk=False)
    d_pool = Disk(by_id="/dev/disk/by-id/ata-POOL", serial="SP", size_bytes=100, disk_type="hdd", is_os_disk=False)
    d_free = Disk(by_id="/dev/disk/by-id/ata-FREE", serial="SF", size_bytes=100, disk_type="hdd", is_os_disk=False)
    d_os = Disk(by_id="/dev/disk/by-id/ata-OS", serial="SO", size_bytes=100, disk_type="hdd", is_os_disk=True)
    db_session.add_all([d_media, d_pool, d_free, d_os])
    db_session.commit()

    zfs = MagicMock()
    zfs.get_pool_members = AsyncMock(
        return_value={"/dev/disk/by-id/ata-POOL-part1": "tank"},
    )
    service = SystemRestoreService(zfs=zfs)
    monkeypatch.setattr(sr, "get_device_path", lambda d: "/dev/sda")
    monkeypatch.setattr(sr, "read_slot_uuids", AsyncMock(return_value={
        "/dev/sda": {"partitions": [
            {"name": "sda1", "partlabel": "nazman:SLOT1", "slot_uuid": "SLOT1", "size_bytes": 50},
            {"name": "sda2", "partlabel": None, "slot_uuid": None, "size_bytes": 60},
        ]},
    }))

    attached = await service._attached_disks(db_session, {"ata-MEDIA"})
    assert [d["disk_id"] for d in attached] == [d_free.id]
    assert attached[0]["partitions"] == [
        {"number": 1, "slot_uuid": "SLOT1",
         "device_path": "/dev/disk/by-id/ata-FREE-part1", "size_bytes": 50},
    ]


@pytest.mark.asyncio
async def test_attached_disks_keeps_free_partitions_of_partially_pooled_disk(db_session, monkeypatch):
    import nazman.services.system_restore as sr
    from nazman.models.disk import Disk

    d_part = Disk(by_id="/dev/disk/by-id/ata-PART", serial="SP", size_bytes=100, disk_type="hdd", is_os_disk=False)
    d_whole = Disk(by_id="/dev/disk/by-id/ata-WHOLE", serial="SW", size_bytes=100, disk_type="hdd", is_os_disk=False)
    db_session.add_all([d_part, d_whole])
    db_session.commit()

    zfs = MagicMock()
    zfs.get_pool_members = AsyncMock(return_value={
        "/dev/disk/by-id/ata-PART-part2": "fast",
        "/dev/disk/by-id/ata-WHOLE": "slow",
    })
    service = SystemRestoreService(zfs=zfs)
    monkeypatch.setattr(sr, "get_device_path", lambda d: "/dev/sda")
    monkeypatch.setattr(sr, "read_slot_uuids", AsyncMock(return_value={
        "/dev/sda": {"partitions": [
            {"name": "sda1", "partlabel": "nazman:SLOT1", "slot_uuid": "SLOT1", "size_bytes": 50},
            {"name": "sda2", "partlabel": "nazman:SLOT2", "slot_uuid": "SLOT2", "size_bytes": 60},
            {"name": "sda3", "partlabel": "nazman:SLOT3", "slot_uuid": "SLOT3", "size_bytes": 70},
        ]},
    }))

    attached = await service._attached_disks(db_session, set())
    assert [d["disk_id"] for d in attached] == [d_part.id]
    assert attached[0]["in_pool"] is True
    assert attached[0]["member_slot_uuids"] == ["SLOT2"]
    assert [p["slot_uuid"] for p in attached[0]["partitions"]] == ["SLOT1", "SLOT3"]


@pytest.mark.asyncio
async def test_plan_pool_mapping_uses_free_partitions_of_pooled_disk(db_session):
    service = SystemRestoreService()
    manifest = {
        "pools": [{
            "name": "fast", "ashift": 12,
            "vdevs": [{
                "role": "data", "topology": "mirror", "ashift": 12,
                "devices": [
                    {"by_id": "/dev/disk/by-id/ata-PART", "serial": "SP", "size_bytes": 100,
                     "slot_uuid": "SLOT2", "partition_number": 2},
                    {"by_id": "/dev/disk/by-id/ata-PART", "serial": "SP", "size_bytes": 100,
                     "slot_uuid": "SLOT1", "partition_number": 1},
                    {"by_id": "/dev/disk/by-id/ata-PART", "serial": "SP", "size_bytes": 100,
                     "slot_uuid": "SLOT3", "partition_number": 3},
                    {"by_id": "/dev/disk/by-id/ata-PART", "serial": "SP", "size_bytes": 100},
                ],
            }],
        }],
    }
    service._manifest_for = AsyncMock(return_value=manifest)
    service._attached_disks = AsyncMock(return_value=[
        {"disk_id": 1, "by_id": "/dev/disk/by-id/ata-PART", "serial": "SP", "size_bytes": 100,
         "model": "P", "device_path": "/dev/sda", "present": True, "is_os_disk": False,
         "in_pool": True, "member_slot_uuids": ["SLOT2"],
         "partitions": [
             {"number": 1, "slot_uuid": "SLOT1",
              "device_path": "/dev/disk/by-id/ata-PART-part1", "size_bytes": 50},
             {"number": 3, "slot_uuid": "SLOT3",
              "device_path": "/dev/disk/by-id/ata-PART-part3", "size_bytes": 50},
         ]},
    ])

    plan = await service.plan_pool_mapping(db_session, "AAA", "fast")
    devices = plan["vdevs"][0]["devices"]
    assert devices[0]["matched_disk_id"] is None
    assert devices[1]["matched_partition"]["slot_uuid"] == "SLOT1"
    assert devices[2]["matched_partition"]["slot_uuid"] == "SLOT3"
    assert devices[3]["matched_disk_id"] is None
    assert plan["available_disks"] == []


@pytest.mark.asyncio
async def test_plan_pool_mapping_blocks_whole_disk_after_partition_used(db_session):
    service = SystemRestoreService()
    manifest = {
        "pools": [{
            "name": "fast", "ashift": 12,
            "vdevs": [{
                "role": "data", "topology": "mirror", "ashift": 12,
                "devices": [
                    {"by_id": "/dev/disk/by-id/ata-OTHER", "serial": "SO", "size_bytes": 100,
                     "slot_uuid": "SLOT9", "partition_number": 1},
                    {"by_id": "/dev/disk/by-id/ata-OTHER", "serial": "SO", "size_bytes": 100},
                ],
            }],
        }],
    }
    service._manifest_for = AsyncMock(return_value=manifest)
    service._attached_disks = AsyncMock(return_value=[
        {"disk_id": 7, "by_id": "/dev/disk/by-id/ata-OTHER", "serial": "SO", "size_bytes": 100,
         "model": "O", "device_path": "/dev/sda", "present": True, "is_os_disk": False,
         "in_pool": False, "member_slot_uuids": [],
         "partitions": [{"number": 1, "slot_uuid": "SLOT9",
                         "device_path": "/dev/disk/by-id/ata-OTHER-part1", "size_bytes": 50}]},
    ])

    plan = await service.plan_pool_mapping(db_session, "AAA", "fast")
    devices = plan["vdevs"][0]["devices"]
    assert devices[0]["matched_partition"]["slot_uuid"] == "SLOT9"
    assert devices[1]["matched_disk_id"] is None
    assert plan["available_disks"] == []


@pytest.mark.asyncio
async def test_restore_plan_defaults_enabled_with_matching_pool(db_session):
    zfs = MagicMock()
    zfs.list_pool_names = MagicMock(return_value=["tank"])
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
        "_volumes": [
            {"volume_id": "AAA", "candidate": _candidate("AAA")},
        ],
    })

    media = await service.required_media(db_session, "AAA")
    assert media[0]["media_fs_uuid"] == "AAA"
    assert media[0]["connected"] is True
    assert media[0]["datasets"] == ["tank/media"]


@pytest.mark.asyncio
async def test_required_media_marks_a_missing_volume_disconnected(db_session):
    """A set that spans two disks reports the one that is not attached."""
    service = SystemRestoreService()
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{
            "name": "tank/media",
            "backups": [
                {"type": "full", "stream_file": "a", "created_at": "2026-01-01T00:00:00",
                 "media_fs_uuid": "AAA", "media_label": "vol1"},
                {"type": "incremental", "stream_file": "b", "created_at": "2026-01-02T00:00:00",
                 "media_fs_uuid": "BBB", "media_label": "vol2"},
            ],
        }],
        "_volumes": [{"volume_id": "AAA", "candidate": _candidate("AAA")}],
    })

    media = {m["media_fs_uuid"]: m for m in await service.required_media(db_session, "1")}
    assert media["AAA"]["connected"] is True
    assert media["BBB"]["connected"] is False


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
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service.zfs_backup.receive_stream = AsyncMock(return_value={"ok": True})
    service._mount_volumes = AsyncMock(return_value=[(_candidate("AAA"), volume)])
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
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service.zfs_backup.receive_stream = AsyncMock()
    service._mount_volumes = AsyncMock(return_value=[(_candidate("AAA"), tmp_path)])
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
    zfs.list_pool_names = MagicMock(return_value=["tank"])
    service = SystemRestoreService(zfs=zfs)
    with pytest.raises(ValidationError):
        await service.create_pool_from_backup(
            db_session, "AAA", "tank",
            [{"role": "data", "topology": "stripe", "devices": [{"disk_id": 1}]}],
        )


@pytest.mark.asyncio
async def test_create_pool_from_backup_calls_zfs(db_session):
    zfs = MagicMock()
    zfs.list_pool_names = MagicMock(return_value=[])
    zfs.create_pool = AsyncMock(return_value={"name": "tank"})
    service = SystemRestoreService(zfs=zfs)
    vdevs = [{"role": "data", "topology": "stripe", "devices": [{"disk_id": 1, "slot_uuid": None}]}]
    out = await service.create_pool_from_backup(db_session, "AAA", "tank", vdevs)
    assert out["name"] == "tank"
    zfs.create_pool.assert_awaited_once_with(db_session, "tank", vdevs)


@pytest.mark.asyncio
async def test_adopt_media_reports_volumes_it_cannot_adopt(db_session):
    """A volume with no filesystem UUID cannot be declared, and the rest of the
    set is still adopted rather than blocked by it."""
    service = SystemRestoreService()
    service._volumes_of = AsyncMock(return_value=[
        {"volume_id": "dev:sdb1", "candidate": {"device": "/dev/sdb1", "fs_uuid": None}},
    ])

    out = await service.adopt_media(db_session, "dev:sdb1")
    assert out["adopted"] is False
    assert len(out["missing"]) == 1


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
        mock.start_restore = AsyncMock(side_effect=BackupError("boom"))
        response = client.post(
            "/api/system-restore/sets/AAA/datasets/restore",
            json={"selections": []},
        )
    assert response.status_code == 400
    assert "boom" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_restore_start_returns_progress_view(client):
    with override_manager(get_system_restore_service) as mock:
        mock.start_restore = AsyncMock(return_value={
            "set_id": "AAA", "status": "running", "datasets_total": 2,
            "datasets_done": 0, "datasets_failed": 0, "progress_pct": 0,
            "current": None, "results": [], "error": None, "started_at": None,
        })
        response = client.post(
            "/api/system-restore/sets/AAA/datasets/restore",
            json={"selections": [{"source_dataset": "tank/media", "target_pool": "tank"}]},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["datasets_total"] == 2


@pytest.mark.asyncio
async def test_api_restore_conflict_returns_409(client):
    with override_manager(get_system_restore_service) as mock:
        mock.start_restore = AsyncMock(side_effect=ConflictError("already running"))
        response = client.post(
            "/api/system-restore/sets/AAA/datasets/restore",
            json={"selections": []},
        )
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_restore_progress_returns_view(client):
    with override_manager(get_system_restore_service) as mock:
        mock.restore_job_view = MagicMock(return_value={
            "set_id": "AAA", "status": "running", "progress_pct": 42,
        })
        response = client.get("/api/system-restore/sets/AAA/restore/progress")
    assert response.status_code == 200
    assert response.json()["progress_pct"] == 42
    mock.restore_job_view.assert_called_once_with("AAA")


@pytest.mark.asyncio
async def test_api_dataset_restore_plan(client):
    with override_manager(get_system_restore_service) as mock:
        mock.restore_plan = AsyncMock(return_value=[
            {"source_dataset": "tank/media", "enabled": True},
        ])
        response = client.get("/api/system-restore/sets/AAA/datasets/plan")
    assert response.status_code == 200
    assert response.json()[0]["source_dataset"] == "tank/media"


@pytest.mark.asyncio
async def test_discover_backup_sets_groups_volumes_by_set_stamp(db_session, tmp_path):
    """Two volumes stamped as one set are offered as a single backup set."""
    vol1 = tmp_path / "vol1"
    vol2 = tmp_path / "vol2"
    vol1.mkdir()
    vol2.mkdir()
    _write_volume(vol1, media_uuid="AAA", set_id=7)
    _write_volume(vol2, media_uuid="BBB", set_id=7, stream="data/tank/media/incr-1.zfs.gz")

    service = SystemRestoreService()
    service.list_candidates = AsyncMock(return_value=[
        _candidate("AAA"), {**_candidate("BBB"), "device": "/dev/sdc1",
                            "device_name": "sdc1", "label": "vol2"},
    ])
    service._mount_readonly = AsyncMock(side_effect=[vol1, vol2, vol1, vol2])
    service._unmount = AsyncMock()

    sets = await service.discover_backup_sets(db_session)
    assert len(sets) == 1
    assert sets[0]["set_id"] == "7"
    assert sets[0]["volume_count"] == 2
    assert {v["fs_uuid"] for v in sets[0]["volumes"]} == {"AAA", "BBB"}


@pytest.mark.asyncio
async def test_discover_backup_sets_keeps_unstamped_volumes_separate(db_session, tmp_path):
    """Media written before backup sets existed is still one set per volume."""
    vol1 = tmp_path / "vol1"
    vol2 = tmp_path / "vol2"
    vol1.mkdir()
    vol2.mkdir()
    _write_volume(vol1, media_uuid="AAA")
    _write_volume(vol2, media_uuid="BBB", stream="data/tank/media/incr-1.zfs.gz")

    service = SystemRestoreService()
    service.list_candidates = AsyncMock(return_value=[
        _candidate("AAA"), {**_candidate("BBB"), "device": "/dev/sdc1",
                            "device_name": "sdc1", "label": "vol2"},
    ])
    service._mount_readonly = AsyncMock(side_effect=[vol1, vol2, vol1, vol2])
    service._unmount = AsyncMock()

    sets = await service.discover_backup_sets(db_session)
    assert {s["set_id"] for s in sets} == {"AAA", "BBB"}
    assert all(s["volume_count"] == 1 for s in sets)


def _scan_service(tmp_path, volumes):
    """A scanner whose mounts resolve each candidate UUID to a volume dir."""
    by_uuid = {vol_fs: path for path, vol_fs in volumes}

    async def mount_readonly(candidate):
        return by_uuid[candidate["fs_uuid"]]

    service = SystemRestoreService()
    service.list_candidates = AsyncMock(return_value=[
        _candidate(fs) for _, fs in volumes
    ])
    service._mount_readonly = mount_readonly
    service._unmount = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_discover_backup_sets_marks_latest_per_group(db_session, tmp_path):
    vol_a = tmp_path / "a"; vol_a.mkdir()
    vol_b = tmp_path / "b"; vol_b.mkdir()
    vol_c = tmp_path / "c"; vol_c.mkdir()
    _write_volume(vol_a, media_uuid="AAA", set_id=101, group="Media",
                  updated_at="2026-02-01T00:00:00+00:00")
    _write_volume(vol_b, media_uuid="BBB", set_id=102, group="Media",
                  updated_at="2026-02-08T00:00:00+00:00")
    _write_volume(vol_c, media_uuid="CCC", set_id=201, group="Docs",
                  updated_at="2026-01-15T00:00:00+00:00")

    service = _scan_service(tmp_path, [(vol_a, "AAA"), (vol_b, "BBB"), (vol_c, "CCC")])
    sets = await service.discover_backup_sets(db_session)

    media = sorted([s for s in sets if s["group"] == "Media"], key=lambda s: s["set_id"])
    assert [s["set_id"] for s in media] == ["101", "102"]
    assert [s["set_id"] for s in media if s["is_latest"]] == ["102"]
    docs = [s for s in sets if s["group"] == "Docs"]
    assert docs and docs[0]["is_latest"] is True


@pytest.mark.asyncio
async def test_discover_backup_sets_marks_every_latest_tie(db_session, tmp_path):
    """Equal timestamps (e.g. after replication) are all marked latest."""
    vol_a = tmp_path / "a"; vol_a.mkdir()
    vol_b = tmp_path / "b"; vol_b.mkdir()
    _write_volume(vol_a, media_uuid="AAA", set_id=101, group="Media",
                  updated_at="2026-02-08T00:00:00+00:00")
    _write_volume(vol_b, media_uuid="BBB", set_id=102, group="Media",
                  updated_at="2026-02-08T00:00:00+00:00")

    service = _scan_service(tmp_path, [(vol_a, "AAA"), (vol_b, "BBB")])
    sets = await service.discover_backup_sets(db_session)

    assert sorted(s["set_id"] for s in sets if s["is_latest"]) == ["101", "102"]


@pytest.mark.asyncio
async def test_discover_backup_sets_unstamped_volume_is_never_latest(db_session, tmp_path):
    vol_a = tmp_path / "a"; vol_a.mkdir()
    _write_volume(vol_a, media_uuid="AAA", updated_at="2026-02-08T00:00:00+00:00")

    service = _scan_service(tmp_path, [(vol_a, "AAA")])
    sets = await service.discover_backup_sets(db_session)

    assert len(sets) == 1
    assert sets[0]["group"] is None
    assert sets[0]["is_latest"] is False


@pytest.mark.asyncio
async def test_restore_datasets_replays_a_chain_that_spans_volumes(db_session, tmp_path):
    """A set's chain continues onto the next disk, so the replay reads each
    stream from whichever volume of the set holds it."""
    vol1 = tmp_path / "vol1"
    vol2 = tmp_path / "vol2"
    (vol1 / "data/tank/media").mkdir(parents=True)
    (vol2 / "data/tank/media").mkdir(parents=True)
    (vol1 / "data/tank/media/full-1.zfs.gz").write_bytes(b"x")
    (vol2 / "data/tank/media/incr-2.zfs.gz").write_bytes(b"x")

    chain = [
        {"type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
         "created_at": "2026-01-01T00:00:00", "media_fs_uuid": "AAA", "set_id": 7},
        {"type": "incremental", "stream_file": "data/tank/media/incr-2.zfs.gz",
         "created_at": "2026-01-02T00:00:00", "media_fs_uuid": "BBB", "set_id": 7},
    ]
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{"name": "tank/media", "pool": "tank", "backups": chain}],
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service.zfs_backup.receive_stream = AsyncMock(return_value={"ok": True})
    service._mount_volumes = AsyncMock(return_value=[
        (_candidate("AAA"), vol1), (_candidate("BBB"), vol2),
    ])
    service._unmount = AsyncMock()

    result = await service.restore_datasets(
        db_session, "7",
        [{"source_dataset": "tank/media", "target_pool": "newtank", "enabled": True}],
    )

    calls = service.zfs_backup.receive_stream.await_args_list
    # The full comes off the first volume, the incremental off the second.
    assert [str(c.args[0]) for c in calls] == [
        str(vol1 / "data/tank/media/full-1.zfs.gz"),
        str(vol2 / "data/tank/media/incr-2.zfs.gz"),
    ]
    assert result["results"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_restore_datasets_reports_a_stream_whose_volume_is_absent(db_session, tmp_path):
    """A missing disk fails that dataset with a clear reason, not a traceback."""
    vol1 = tmp_path / "vol1"
    (vol1 / "data/tank/media").mkdir(parents=True)
    (vol1 / "data/tank/media/full-1.zfs.gz").write_bytes(b"x")

    chain = [
        {"type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
         "created_at": "2026-01-01T00:00:00", "media_fs_uuid": "AAA", "set_id": 7},
        {"type": "incremental", "stream_file": "data/tank/media/incr-2.zfs.gz",
         "created_at": "2026-01-02T00:00:00", "media_fs_uuid": "BBB", "set_id": 7},
    ]
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{"name": "tank/media", "pool": "tank", "backups": chain}],
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service.zfs_backup.receive_stream = AsyncMock()
    service._mount_volumes = AsyncMock(return_value=[(_candidate("AAA"), vol1)])
    service._unmount = AsyncMock()

    result = await service.restore_datasets(
        db_session, "7",
        [{"source_dataset": "tank/media", "target_pool": "newtank", "enabled": True}],
    )

    entry = result["results"][0]
    assert entry["status"] == "failed"
    assert "incr-2.zfs.gz" in entry["error"]
    assert "BBB" in entry["error"]


@pytest.mark.asyncio
async def test_restore_plan_flags_a_chain_that_spans_volumes(db_session):
    service = SystemRestoreService(zfs=MagicMock())
    service.zfs.list_pool_names = MagicMock(return_value=["tank"])
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{
            "name": "tank/media", "pool": "tank",
            "backups": [
                {"type": "full", "stream_file": "a", "created_at": "2026-01-01T00:00:00",
                 "media_fs_uuid": "AAA"},
                {"type": "incremental", "stream_file": "b", "created_at": "2026-01-02T00:00:00",
                 "media_fs_uuid": "BBB"},
            ],
        }],
        "_volumes": [{"volume_id": "AAA", "candidate": _candidate("AAA")}],
    })

    plan = await service.restore_plan(db_session, "7")
    assert plan[0]["spans_volumes"] is True
    assert plan[0]["media_missing"] == ["BBB"]


@pytest.mark.asyncio
async def test_merge_manifests_unions_datasets_runs_pools_and_configs():
    merged = SystemRestoreService._merge_manifests([
        {
            "datasets": [{"name": "tank/a", "pool": "tank", "backups": [
                {"type": "full", "stream_file": "s1", "created_at": "2026-01-01T00:00:00",
                 "media_fs_uuid": "AAA"},
            ]}],
            "pools": [{"name": "tank"}],
            "config_backups": [{"id": "c1"}],
        },
        {
            "datasets": [
                {"name": "tank/a", "pool": "tank", "backups": [
                    {"type": "full", "stream_file": "s1", "created_at": "2026-01-01T00:00:00",
                     "media_fs_uuid": "AAA"},
                    {"type": "incremental", "stream_file": "s2",
                     "created_at": "2026-01-02T00:00:00", "media_fs_uuid": "BBB"},
                ]},
                {"name": "tank/b", "pool": "tank", "backups": []},
            ],
            "pools": [{"name": "tank"}],
            "config_backups": [{"id": "c2"}],
        },
    ])

    names = [d["name"] for d in merged["datasets"]]
    assert names == ["tank/a", "tank/b"]
    # The duplicated run is keyed by stream file, so it is not doubled up.
    assert [r["stream_file"] for r in merged["datasets"][0]["backups"]] == ["s1", "s2"]
    assert [p["name"] for p in merged["pools"]] == ["tank"]
    assert [c["id"] for c in merged["config_backups"]] == ["c1", "c2"]


def _progress_job(**overrides):
    job = {
        "set_id": "AAA", "status": "running", "datasets_total": 4,
        "datasets_done": 0, "datasets_failed": 0,
        "started_at": None, "error": None, "results": [], "current": None,
    }
    job.update(overrides)
    return job


def test_restore_progress_pct_counts_the_current_stream():
    service = SystemRestoreService()
    job = _progress_job(
        datasets_done=1,
        current={"streams_total": 2, "streams_done": 0,
                 "bytes_expected": 100, "bytes_done": 50},
    )
    assert service._progress_pct(job) == 31


def test_restore_progress_pct_caps_running_at_99():
    service = SystemRestoreService()
    job = _progress_job(
        datasets_total=1, datasets_done=0,
        current={"streams_total": 1, "streams_done": 0,
                 "bytes_expected": 100, "bytes_done": 100},
    )
    assert service._progress_pct(job) == 99


def test_restore_progress_pct_is_100_when_done():
    service = SystemRestoreService()
    assert service._progress_pct(_progress_job(status="done", datasets_done=4)) == 100


def test_log_restore_finished_journals_duration_and_bytes():
    service = SystemRestoreService()
    started = datetime.now(timezone.utc)
    job = {
        "status": "done", "started_at": started,
        "datasets_done": 2, "datasets_total": 2, "datasets_failed": 0,
        "bytes_total": 4096, "error": None,
    }
    with patch("nazman.services.system_restore.notification_store") as store:
        service._log_restore_finished(job)
    kw = store.add.call_args.kwargs
    assert kw["level"] == "success"
    assert kw["source"] == "restore"
    assert kw["bytes"] == 4096
    assert kw["duration_ms"] is not None
    assert "2/2 datasets" in kw["message"]


def test_log_restore_finished_marks_failure():
    service = SystemRestoreService()
    job = {
        "status": "failed", "started_at": datetime.now(timezone.utc),
        "datasets_done": 1, "datasets_total": 2, "datasets_failed": 1,
        "bytes_total": 0, "error": "disk vanished",
    }
    with patch("nazman.services.system_restore.notification_store") as store:
        service._log_restore_finished(job)
    kw = store.add.call_args.kwargs
    assert kw["level"] == "error"
    assert "disk vanished" in kw["message"]
    assert kw["bytes"] is None


def test_restore_active_jobs_reflects_running_state():
    service = SystemRestoreService()
    assert service.active_jobs() == []
    service._restore_job = {
        "set_id": "AAA", "status": "running", "datasets_total": 4,
        "datasets_done": 1, "datasets_failed": 0, "started_at": None,
        "error": None, "results": [], "current": None,
    }
    jobs = service.active_jobs()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "restore"
    assert jobs[0]["id"] == "AAA"
    service._restore_job["status"] = "done"
    assert service.active_jobs() == []


def test_restore_job_view_is_idle_without_a_job():
    service = SystemRestoreService()
    assert service.restore_job_view("AAA") == {"set_id": "AAA", "status": "idle"}
    assert service.restore_job_view() == {"set_id": None, "status": "idle"}


@pytest.mark.asyncio
async def test_start_restore_conflicts_while_running(db_session):
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._restore_job = _progress_job(status="running")
    with pytest.raises(ConflictError):
        await service.start_restore(db_session, "AAA", [])


@pytest.mark.asyncio
async def test_start_restore_rejects_empty_selection(db_session):
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{"name": "tank/media", "pool": "tank", "backups": [
            {"type": "full", "stream_file": "s", "created_at": "2026-01-01T00:00:00",
             "media_fs_uuid": "AAA"},
        ]}],
    })
    with pytest.raises(ValidationError):
        await service.start_restore(db_session, "AAA", [
            {"source_dataset": "tank/media", "target_pool": "tank", "enabled": False},
        ])


@pytest.mark.asyncio
async def test_start_restore_runs_worker_and_publishes_progress(db_session, tmp_path):
    media_dir = tmp_path / "data/tank/media"
    media_dir.mkdir(parents=True)
    (media_dir / "full-1.zfs.gz").write_bytes(b"x")

    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{"name": "tank/media", "pool": "tank", "backups": [
            {"type": "full", "stream_file": "data/tank/media/full-1.zfs.gz",
             "created_at": "2026-01-01T00:00:00", "media_fs_uuid": "AAA",
             "size_bytes": 4096},
        ]}],
    })
    service.zfs.dataset_exists = AsyncMock(return_value=False)
    service._mount_volumes = AsyncMock(return_value=[(_candidate("AAA"), tmp_path)])
    service._unmount = AsyncMock()
    snapshots = []

    async def fake_receive(stream, target, force=False, on_bytes_read=None):
        if on_bytes_read is not None:
            on_bytes_read(4096)
        snapshots.append(dict(service.restore_job_view("AAA")["current"]))
        return {"ok": True}

    service.zfs_backup.receive_stream = fake_receive

    started = await service.start_restore(db_session, "AAA", [
        {"source_dataset": "tank/media", "target_pool": "newtank", "enabled": True},
    ])
    assert started["status"] == "running"
    assert started["datasets_total"] == 1
    assert started["current"] is None

    await service._restore_task

    assert snapshots and snapshots[0]["bytes_done"] == 4096
    assert snapshots[0]["source"] == "tank/media"
    done = service.restore_job_view("AAA")
    assert done["status"] == "done"
    assert done["progress_pct"] == 100
    assert done["results"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_start_restore_marks_job_failed_when_mounting_fails(db_session):
    service = SystemRestoreService(zfs=MagicMock(), zfs_backup=MagicMock())
    service._manifest_for = AsyncMock(return_value={
        "datasets": [{"name": "tank/media", "pool": "tank", "backups": [
            {"type": "full", "stream_file": "s", "created_at": "2026-01-01T00:00:00",
             "media_fs_uuid": "AAA"},
        ]}],
    })
    service._mount_volumes = AsyncMock(side_effect=BackupError("no media"))

    await service.start_restore(db_session, "AAA", [
        {"source_dataset": "tank/media", "target_pool": "tank", "enabled": True},
    ])
    await service._restore_task

    view = service.restore_job_view("AAA")
    assert view["status"] == "failed"
    assert "no media" in view["error"]
