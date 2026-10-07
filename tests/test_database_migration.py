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


def test_migrate_backup_tables_adds_group_rotation_columns(tmp_path):
    """A pre-install backup_groups table gains copies + recycle_full_disks."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_groups ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "name VARCHAR NOT NULL,"
            "full_cron VARCHAR, incremental_cron VARCHAR,"
            "enabled BOOLEAN DEFAULT 1, active_set_id INTEGER,"
            "needs_disk BOOLEAN DEFAULT 0,"
            "last_session_at DATETIME, created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text("INSERT INTO backup_groups (name) VALUES ('Weekly')"))

    engine2 = _engine(tmp_path)
    with engine2.connect() as conn:
        migrate_backup_tables(engine2, conn)
        cols = {c["name"] for c in inspect(engine2).get_columns("backup_groups")}
        row = conn.execute(
            text("SELECT name, copies, recycle_full_disks FROM backup_groups")
        ).fetchone()

    assert {"copies", "recycle_full_disks"} <= cols
    assert row is not None
    assert row.name == "Weekly"
    assert row.copies == 1
    assert row.recycle_full_disks == 0


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


def test_migrate_backup_tables_adds_sha256_to_backup_runs(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "dataset_name VARCHAR NOT NULL,"
            "backup_disk_id INTEGER NOT NULL,"
            "backup_type VARCHAR NOT NULL,"
            "status VARCHAR DEFAULT 'running')"
        ))
    engine2 = _engine(tmp_path)
    with engine2.connect() as conn:
        migrate_backup_tables(engine2, conn)
        cols = {c["name"] for c in inspect(engine2).get_columns("backup_runs")}
    assert "sha256" in cols


def test_migrate_backup_tables_adds_estimated_bytes_to_backup_runs(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "dataset_name VARCHAR NOT NULL,"
            "backup_disk_id INTEGER NOT NULL,"
            "backup_type VARCHAR NOT NULL,"
            "status VARCHAR DEFAULT 'running')"
        ))
    engine2 = _engine(tmp_path)
    with engine2.connect() as conn:
        migrate_backup_tables(engine2, conn)
        cols = {c["name"] for c in inspect(engine2).get_columns("backup_runs")}
    assert "estimated_bytes" in cols
    assert cols.issuperset({"phase", "sha256"})  # other nullable evolutions still land


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


def test_run_migrations_drops_obsolete_backup_commits(tmp_path):
    """The git-era backup_commits table is dropped on startup."""
    from nazman.migrations import run_migrations

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE backup_commits (id INTEGER PRIMARY KEY, commit_hash VARCHAR)"
        ))
    with engine.connect() as conn:
        run_migrations(engine, conn)
    assert not inspect(engine).has_table("backup_commits")


def test_migrate_backup_tables_idempotent_and_missing_tables_ok(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        migrate_backup_tables(engine, conn)  # no tables at all
    with engine.connect() as conn:
        migrate_backup_tables(engine, conn)  # run twice
    assert not inspect(engine).has_table("backup_disks")
    assert not inspect(engine).has_table("backup_runs")


def _seed_legacy_backup_state(conn):
    """A pre-upgrade database: schedules, disks, runs and their cron jobs."""
    conn.execute(text(
        "CREATE TABLE backup_schedules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "dataset_name VARCHAR NOT NULL, backup_disk_id INTEGER NOT NULL,"
        "full_cron VARCHAR, incremental_cron VARCHAR,"
        "full_retention INTEGER, incremental_retention INTEGER,"
        "enabled BOOLEAN DEFAULT 1, "
        "UNIQUE (dataset_name, backup_disk_id))"
    ))
    conn.execute(text(
        "CREATE TABLE backup_disks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "disk_id INTEGER NOT NULL, mount_point VARCHAR NOT NULL, fs_uuid VARCHAR NOT NULL)"
    ))
    conn.execute(text(
        "CREATE TABLE backup_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_name VARCHAR NOT NULL,"
        "backup_disk_id INTEGER, backup_type VARCHAR)"
    ))
    conn.execute(text(
        "CREATE TABLE scheduled_tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR NOT NULL,"
        "task_type VARCHAR NOT NULL, target VARCHAR NOT NULL, schedule VARCHAR NOT NULL)"
    ))
    conn.execute(text("INSERT INTO backup_disks (disk_id, mount_point, fs_uuid) "
                      "VALUES (7, '/mnt/backup/ABC', 'ABC')"))
    conn.execute(text("INSERT INTO backup_schedules "
                      "(dataset_name, backup_disk_id, full_cron) "
                      "VALUES ('tank/media', 1, '0 2 * * *')"))
    conn.execute(text("INSERT INTO backup_runs (dataset_name, backup_disk_id, backup_type) "
                      "VALUES ('tank/media', 1, 'full')"))
    conn.execute(text(
        "INSERT INTO scheduled_tasks (name, task_type, target, schedule) VALUES "
        "('zfs-full-tank/media-1', 'zfs_backup', 'tank/media', '0 2 * * *'), "
        "('scrub-tank', 'scrub', 'tank', '0 4 * * 0')"
    ))


def test_run_migrations_discards_legacy_backup_state(tmp_path):
    """The pre-rotation schedule/disk/run state is dropped, not half-migrated."""
    from nazman.database import Base
    from nazman import models  # noqa: F401  (register the tables on Base)
    from nazman.migrations import run_migrations

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _seed_legacy_backup_state(conn)

    with engine.connect() as conn:
        run_migrations(engine, conn)
    # init_db creates the (now empty) tables right after migrating.
    Base.metadata.create_all(bind=engine)

    assert not inspect(engine).has_table("backup_schedules")
    assert inspect(engine).has_table("backup_groups")
    assert inspect(engine).has_table("backup_sets")
    assert inspect(engine).has_table("backup_sessions")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM backup_disks")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM backup_runs")).scalar() == 0
        # Only the backup jobs go; unrelated scheduled work is untouched.
        left = {r.task_type for r in conn.execute(text("SELECT task_type FROM scheduled_tasks"))}
    assert left == {"scrub"}


def test_legacy_backup_reset_runs_only_once(tmp_path):
    """After the first reset there is no marker table, so a second run is a
    no-op and re-declared backup state survives it."""
    from nazman.database import Base
    from nazman import models  # noqa: F401
    from nazman.migrations import run_migrations

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _seed_legacy_backup_state(conn)
    with engine.connect() as conn:
        run_migrations(engine, conn)
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO backup_disks (disk_id, partition_number, mount_point, fs_uuid) "
            "VALUES (8, 1, '/mnt/backup/XYZ', 'XYZ')"
        ))
    with engine.connect() as conn:
        run_migrations(engine, conn)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT fs_uuid FROM backup_disks")).fetchone()
    assert row is not None
    assert row.fs_uuid == "XYZ"
