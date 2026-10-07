import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nazman.managers.backup_manager import BackupManager
from nazman.utils import backup_manifest as bm
from nazman.utils.exceptions import BackupError


def _manager():
    zfs = MagicMock()
    zfs.get_pool_recreate_specs = AsyncMock(return_value=[
        {"name": "tank", "ashift": 12, "vdevs": []},
    ])
    zfs.get_dataset_recreate_specs = AsyncMock(return_value=[
        {"name": "tank/media", "pool": "tank", "properties": {"compression": "zstd"}, "mountpoint": "/tank/media"},
    ])
    return BackupManager(zfs=zfs)


@pytest.mark.asyncio
async def test_capture_config_bundle_writes_bundle_manifest_and_sidecar(db_session, tmp_path):
    manager = _manager()
    volume = tmp_path / "vol"
    entry = await manager.capture_config_bundle(
        db_session, volume, media={"fs_uuid": "AAA", "label": "backup1"},
    )

    bundle = volume / bm.CONFIG_DIR / entry["id"]
    assert bundle.is_dir()
    assert (bundle / "system-config").is_dir()

    manifest = bm.load_manifest(volume)
    assert manifest is not None
    assert manifest["media"]["fs_uuid"] == "AAA"
    assert manifest["pools"][0]["name"] == "tank"
    assert [d["name"] for d in manifest["datasets"]] == ["tank/media"]
    assert [c["id"] for c in manifest["config_backups"]] == [entry["id"]]

    sidecar = json.loads((bundle / f"config{bm.SIDECAR_SUFFIX}").read_text())
    assert sidecar["kind"] == "config"
    assert sidecar["config"]["id"] == entry["id"]


def test_prune_config_bundles_keeps_newest(tmp_path, override_settings):
    override_settings.backup_config_retention = 2
    with patch("nazman.managers.backup_manager.get_settings", return_value=override_settings):
        manager = _manager()
    volume = tmp_path / "vol"
    manifest = bm.new_manifest(media={"fs_uuid": "AAA"})
    ids = ["20260101-000000", "20260201-000000", "20260301-000000"]
    for i in ids:
        (volume / bm.CONFIG_DIR / i).mkdir(parents=True)
        bm.upsert_config_backup(manifest, {"id": i, "created_at": i})
    bm.save_manifest(volume, manifest)

    manager._prune_config_bundles(volume, manifest)

    remaining = sorted(p.name for p in (volume / bm.CONFIG_DIR).iterdir())
    assert remaining == ["20260201-000000", "20260301-000000"]
    assert len(bm.load_manifest(volume)["config_backups"]) == 2


@pytest.mark.asyncio
async def test_find_config_bundles_on_declared_volume(db_session, tmp_path):
    from nazman.models.backup_zfs import BackupDisk

    manager = _manager()
    volume = tmp_path / "vol"
    volume.mkdir()
    bd = BackupDisk(disk_id=1, mount_point=str(volume), fs_uuid="AAA", unmount_after_backup=False)
    db_session.add(bd)
    db_session.commit()

    await manager.capture_config_bundle(db_session, volume, media={"fs_uuid": "AAA"})
    bundles = manager.find_config_bundles(db_session)
    assert len(bundles) >= 1
    assert bundles[0]["volume_root"] == str(volume)


@pytest.mark.asyncio
async def test_restore_configuration_bundle_missing_raises(db_session, tmp_path):
    manager = _manager()
    with pytest.raises(BackupError):
        await manager.restore_configuration_bundle(db_session, tmp_path / "nope")


@pytest.mark.asyncio
async def test_find_config_bundles_ignores_undeclared_paths(db_session, tmp_path):
    """Only declared backup volumes are searched; a stray bundle elsewhere is
    never offered (the legacy local path is gone)."""
    manager = _manager()
    stray = tmp_path / "not-a-backup-disk"
    await manager.capture_config_bundle(db_session, stray, media={"fs_uuid": "ZZZ"})
    assert manager.find_config_bundles(db_session) == []
