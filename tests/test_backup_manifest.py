import pytest

from nazman.utils import backup_manifest as bm


def test_new_manifest_shape():
    m = bm.new_manifest(media={"fs_uuid": "AAA"}, nazman_version="0.2.0")
    assert m["manifest_version"] == bm.MANIFEST_VERSION
    assert m["media"] == {"fs_uuid": "AAA"}
    assert m["nazman_version"] == "0.2.0"
    assert m["config_backups"] == []
    assert m["pools"] == []
    assert m["datasets"] == []
    bm.validate_manifest(m)


def test_validate_manifest_rejects_bad_input():
    with pytest.raises(ValueError):
        bm.validate_manifest("nope")
    with pytest.raises(ValueError):
        bm.validate_manifest({"manifest_version": 999})
    with pytest.raises(ValueError):
        bm.validate_manifest({"manifest_version": 1, "datasets": {}})


def test_save_load_roundtrip_and_atomic(tmp_path):
    m = bm.new_manifest(media={"fs_uuid": "AAA"})
    bm.save_manifest(tmp_path, m)
    loaded = bm.load_manifest(tmp_path)
    assert loaded is not None
    assert loaded["media"]["fs_uuid"] == "AAA"
    assert loaded["updated_at"] >= loaded["created_at"]
    # No temp files left behind.
    assert not list(tmp_path.glob(".tmp-*"))


def test_load_manifest_missing_and_invalid(tmp_path):
    assert bm.load_manifest(tmp_path) is None
    (tmp_path / bm.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert bm.load_manifest(tmp_path) is None


def test_upsert_config_backup_replaces_and_sorts():
    m = bm.new_manifest()
    bm.upsert_config_backup(m, {"id": "20260101-000000", "created_at": "2026-01-01T00:00:00"})
    bm.upsert_config_backup(m, {"id": "20260201-000000", "created_at": "2026-02-01T00:00:00"})
    bm.upsert_config_backup(m, {"id": "20260101-000000", "created_at": "2026-01-01T00:00:00", "x": 1})
    assert [e["id"] for e in m["config_backups"]] == ["20260201-000000", "20260101-000000"]
    assert m["config_backups"][1]["x"] == 1


def test_upsert_dataset_backup_merges_and_dedups_runs():
    m = bm.new_manifest()
    ds = {"name": "tank/media", "pool": "tank", "properties": {"compression": "zstd"}, "mountpoint": "/tank/media"}
    bm.upsert_dataset_backup(m, ds, {"stream_file": "data/tank/media/full-1.zfs.gz", "created_at": "2026-01-01T00:00:00"})
    bm.upsert_dataset_backup(m, ds, {"stream_file": "data/tank/media/incr-2.zfs.gz", "created_at": "2026-01-02T00:00:00"})
    bm.upsert_dataset_backup(m, ds, {"stream_file": "data/tank/media/full-1.zfs.gz", "created_at": "2026-01-01T00:00:00", "size_bytes": 5})
    assert len(m["datasets"]) == 1
    backups = m["datasets"][0]["backups"]
    assert [b["stream_file"] for b in backups] == [
        "data/tank/media/full-1.zfs.gz",
        "data/tank/media/incr-2.zfs.gz",
    ]
    assert backups[0]["size_bytes"] == 5


def test_sidecar_roundtrip_and_build_from_sidecars(tmp_path):
    stream = tmp_path / "data" / "tank" / "media" / "full-1.zfs.gz"
    stream.parent.mkdir(parents=True)
    stream.write_bytes(b"x")
    info = {
        "kind": "dataset",
        "dataset": {"name": "tank/media", "pool": "tank"},
        "run": {"stream_file": "data/tank/media/full-1.zfs.gz", "created_at": "2026-01-01T00:00:00"},
    }
    bm.write_sidecar(stream, info)
    assert bm.read_sidecar(stream)["kind"] == "dataset"

    cfg_dir = tmp_path / "config" / "20260101-000000"
    cfg_dir.mkdir(parents=True)
    bm.atomic_write_json(
        cfg_dir / f"config{bm.SIDECAR_SUFFIX}",
        {"kind": "config", "config": {"id": "20260101-000000", "created_at": "2026-01-01T00:00:00"}},
    )

    rebuilt = bm.build_from_sidecars(tmp_path, media={"fs_uuid": "AAA"})
    assert rebuilt["media"]["fs_uuid"] == "AAA"
    assert [d["name"] for d in rebuilt["datasets"]] == ["tank/media"]
    assert [c["id"] for c in rebuilt["config_backups"]] == ["20260101-000000"]


def test_scan_volume_prefers_manifest_and_folds_sidecars(tmp_path):
    m = bm.new_manifest(media={"fs_uuid": "AAA"})
    bm.upsert_dataset_backup(
        m,
        {"name": "tank/media", "pool": "tank"},
        {"stream_file": "data/tank/media/full-1.zfs.gz", "created_at": "2026-01-01T00:00:00"},
    )
    bm.save_manifest(tmp_path, m)

    stream = tmp_path / "data" / "tank" / "media" / "incr-2.zfs.gz"
    stream.parent.mkdir(parents=True)
    stream.write_bytes(b"x")
    bm.write_sidecar(
        stream,
        {
            "kind": "dataset",
            "dataset": {"name": "tank/media", "pool": "tank"},
            "run": {"stream_file": "data/tank/media/incr-2.zfs.gz", "created_at": "2026-01-02T00:00:00"},
        },
    )

    scanned = bm.scan_volume(tmp_path)
    assert scanned["media"]["fs_uuid"] == "AAA"
    backups = scanned["datasets"][0]["backups"]
    assert [b["stream_file"] for b in backups] == [
        "data/tank/media/full-1.zfs.gz",
        "data/tank/media/incr-2.zfs.gz",
    ]


def test_manifest_summary_lists_media_uuids():
    m = bm.new_manifest(media={"fs_uuid": "AAA"})
    m["pools"] = [{"name": "tank"}]
    m["datasets"] = [
        {
            "name": "tank/media",
            "backups": [
                {"stream_file": "a", "media_fs_uuid": "BBB"},
                {"stream_file": "b", "media": {"fs_uuid": "CCC"}},
            ],
        }
    ]
    summary = bm.manifest_summary(m)
    assert summary["pool_names"] == ["tank"]
    assert summary["dataset_count"] == 1
    assert summary["media_fs_uuids"] == ["BBB", "CCC"]


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert bm.sha256_file(str(p)) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
