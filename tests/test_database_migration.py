import logging
import pytest
from sqlalchemy import create_engine, text, inspect

from nazman.migrations import migrate_backup_tables


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")


def test_migrate_backup_tables_preserves_declared_disk(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_disks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "disk_id INTEGER NOT NULL,"
            "slot_uuid VARCHAR, partition_number INTEGER NOT NULL DEFAULT 1,"
            "label VARCHAR, fs_type VARCHAR DEFAULT 'ext4',"
            "mount_point VARCHAR NOT NULL, fs_uuid VARCHAR NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO backup_disks (disk_id, partition_number, mount_point, fs_uuid) "
            "VALUES (7, 1, '/mnt/backup/ABC', 'ABC')"
        ))

    engine2 = _engine(tmp_path)
    with engine2.connect() as conn:
        migrate_backup_tables(engine2, conn)
        cols = {c["name"] for c in inspect(engine2).get_columns("backup_disks")}
        row = conn.execute(text("SELECT fs_uuid, mount_point FROM backup_disks")).fetchone()

    assert "unmount_after_backup" in cols
    assert "created_at" in cols
    assert "updated_at" in cols
    assert row is not None
    assert row.fs_uuid == "ABC"
    assert row.mount_point == "/mnt/backup/ABC"
    indexes = {ix["name"] for ix in inspect(engine2).get_indexes("backup_disks")}
    assert "uq_backup_disk_disk" in indexes
    assert "ix_backup_disks_fs_uuid" in indexes


def test_migrate_backup_tables_adds_phase_to_backup_runs(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "dataset_name VARCHAR NOT NULL,"
            "backup_disk_id INTEGER NOT NULL,"
            "backup_type VARCHAR NOT NULL,"
            "stream_file VARCHAR, snapshot VARCHAR,"
            "size_bytes BIGINT DEFAULT 0, changed_bytes BIGINT DEFAULT 0,"
            "status VARCHAR DEFAULT 'running', error VARCHAR,"
            "started_at DATETIME, completed_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO backup_runs (dataset_name, backup_disk_id, backup_type, status) "
            "VALUES ('tank/data', 2, 'full', 'success')"
        ))

    engine2 = _engine(tmp_path)
    with engine2.connect() as conn:
        migrate_backup_tables(engine2, conn)
        cols = {c["name"] for c in inspect(engine2).get_columns("backup_runs")}
        row = conn.execute(text("SELECT dataset_name, status FROM backup_runs")).fetchone()

    assert "phase" in cols
    assert row is not None
    assert row.dataset_name == "tank/data"
    assert row.status == "success"


def test_migrate_backup_tables_warns_when_skipping_nn_column(tmp_path, caplog):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_disks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "disk_id INTEGER NOT NULL,"
            "slot_uuid VARCHAR, partition_number INTEGER NOT NULL DEFAULT 1,"
            "label VARCHAR, fs_type VARCHAR DEFAULT 'ext4',"
            "fs_uuid VARCHAR NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO backup_disks (disk_id, partition_number, fs_uuid) "
            "VALUES (8, 1, 'DEF')"
        ))

    engine2 = _engine(tmp_path)
    with caplog.at_level(logging.WARNING, logger="nazman.database"):
        with engine2.connect() as conn:
            migrate_backup_tables(engine2, conn)
        cols = {c["name"] for c in inspect(engine2).get_columns("backup_disks")}

    assert "mount_point" not in cols
    assert any("mount_point" in r.message for r in caplog.records)


def test_migrate_backup_tables_idempotent_and_missing_tables_ok(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        migrate_backup_tables(engine, conn)  # no tables at all
    with engine.connect() as conn:
        migrate_backup_tables(engine, conn)  # run twice
    assert not inspect(engine).has_table("backup_disks")
    assert not inspect(engine).has_table("backup_runs")