"""SQLite schema migrations run during database bootstrap.

Kept separate from :mod:`nazman.database` (connection/session plumbing) because
in-place DDL surgery is its own concern.  Each migration is idempotent and
defensive: failures roll back and never block startup of the current schema.
"""

import logging
from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

# Obsolete tables from prior schema versions, dropped unconditionally.
_OBSOLETE_TABLES = (
    "vdevs", "disk_groups", "disk_partitions", "nfs_exports", "datasets",
    "backup_commits",
)


def run_migrations(engine, conn) -> None:
    """Run all schema migrations on an open connection.

    Called with foreign_keys OFF so dropping old tables doesn't fail on FK
    constraints; foreign keys are re-enabled by the caller afterwards.
    """
    _drop_obsolete_tables(conn)
    _rebuild_disks_table(engine, conn)
    _enforce_disk_serial_uniqueness(conn)
    migrate_backup_tables(engine, conn)


def _drop_obsolete_tables(conn) -> None:
    for tbl in _OBSOLETE_TABLES:
        conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
    conn.commit()


def _rebuild_disks_table(engine, conn) -> None:
    """Rebuild the disks table to the current identity model.

    device_name/device_path are ephemeral kernel names and are no longer
    persisted; by_id is now the UNIQUE NOT NULL identity key.  Legacy rows
    without a by_id fall back to their serial (serial used as a stable
    surrogate when available), otherwise they are dropped.
    """
    try:
        inspector = inspect(engine)
        columns = [c["name"] for c in inspector.get_columns("disks")]
        needs_rebuild = (
            "device_name" in columns or "device_path" in columns or "group_id" in columns
        )
        if needs_rebuild:
            conn.execute(text(
                "CREATE TABLE disks_backup AS SELECT * FROM disks"
            ))
            conn.execute(text("DROP TABLE disks"))
            conn.execute(text(
                "CREATE TABLE disks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "by_id VARCHAR NOT NULL UNIQUE,"
                "model VARCHAR, serial VARCHAR,"
                "size_bytes INTEGER NOT NULL,"
                "disk_type VARCHAR NOT NULL,"
                "rotation_speed INTEGER,"
                "health_status VARCHAR DEFAULT 'unknown',"
                "is_os_disk BOOLEAN DEFAULT 0,"
                "status VARCHAR DEFAULT 'active',"
                "temperature INTEGER,"
                "power_on_hours INTEGER,"
                "created_at DATETIME,"
                "updated_at DATETIME"
                ")"
            ))
            conn.execute(text(
                "INSERT INTO disks "
                "(by_id, model, serial, size_bytes, disk_type, rotation_speed,"
                "health_status, is_os_disk, status, temperature, power_on_hours,"
                "created_at, updated_at) "
                "SELECT "
                "  COALESCE(by_id, CASE WHEN serial IS NOT NULL THEN 'serial:' || serial ELSE NULL END),"
                "  model, serial, size_bytes, disk_type, rotation_speed,"
                "  health_status, is_os_disk, status, temperature, power_on_hours,"
                "  created_at, updated_at "
                "FROM disks_backup "
                "WHERE by_id IS NOT NULL OR serial IS NOT NULL "
                "GROUP BY COALESCE(by_id, serial)"
            ))
            conn.execute(text("DROP TABLE disks_backup"))
            conn.commit()
    except Exception:
        conn.rollback()


def _enforce_disk_serial_uniqueness(conn) -> None:
    """Make serial the stable identity fallback behind by_id.

    Enforce uniqueness; rows sharing a serial are neutralised to NULL first
    (duplicate detection on one disk across NAME changes), keeping the
    oldest row so a repeated scan cannot create colliding identity keys.
    """
    try:
        conn.execute(text(
            "UPDATE disks SET serial = NULL "
            "WHERE serial IS NOT NULL AND id NOT IN "
            "(SELECT MIN(id) FROM disks WHERE serial IS NOT NULL GROUP BY serial)"
        ))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS disks_serial_uq ON disks(serial)"))
        conn.commit()
    except Exception:
        conn.rollback()


def migrate_backup_tables(engine, conn) -> None:
    """Migrate backup tables in place so declared disks survive restarts.

    These tables were previously dropped on every startup, which silently
    discarded declared backup disks, schedules and run history.  SQLite can't
    drop columns, but ADD COLUMN covers columns added by schema evolution;
    unique keys are re-created idempotently so dedup guarantees hold even on
    pre-existing tables.
    """
    try:
        from .models import backup_zfs
        backup_specs = [
            ("backup_disks", backup_zfs.BackupDisk, [
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_backup_disk_disk ON backup_disks(disk_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_backup_disks_fs_uuid ON backup_disks(fs_uuid)",
            ]),
            ("backup_schedules", backup_zfs.BackupSchedule, [
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_backup_schedule_dataset_disk "
                "ON backup_schedules(dataset_name, backup_disk_id)",
            ]),
            ("backup_runs", backup_zfs.BackupRun, []),
        ]
        inspector = inspect(engine)
        for tbl, model, index_ddl in backup_specs:
            if not inspector.has_table(tbl):
                continue
            existing = {c["name"] for c in inspector.get_columns(tbl)}
            for col in model.__table__.columns:
                if col.name in existing or col.primary_key:
                    continue
                ddl = (f"ALTER TABLE {tbl} ADD COLUMN {col.name} "
                       f"{col.type.compile(engine.dialect)}")
                default = None
                if col.default is not None and not callable(col.default.arg):
                    default = col.default.arg
                if default is None and not (col.nullable or col.server_default):
                    logger.warning(
                        "cannot add non-nullable column %s.%s to existing table "
                        "(no default); add an explicit migration", tbl, col.name,
                    )
                    continue  # can't add a NOT NULL column to existing rows
                if default is not None:
                    if isinstance(default, bool):
                        default = "1" if default else "0"
                    elif isinstance(default, str):
                        default = f"'{default}'"
                    ddl += f" DEFAULT {default}"
                conn.execute(text(ddl))
            for ddl in index_ddl:
                conn.execute(text(ddl))
        conn.commit()
    except Exception:
        conn.rollback()
