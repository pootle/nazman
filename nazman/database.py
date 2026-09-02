from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from contextlib import contextmanager
from .config import get_settings

engine = None
SessionLocal = None
Base = declarative_base()


def init_db():
    """Initialize database connection and create tables."""
    global engine, SessionLocal

    settings = get_settings()
    database_url = f"sqlite:///{settings.database_path}"

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True
    )

    # Enable WAL mode for better concurrent access
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # ── Schema migrations ──────────────────────────────────────────────
    # Run all migrations with foreign_keys OFF so dropping old tables
    # doesn't fail on FK constraints.  Uses a single raw connection.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))

        # Drop obsolete tables from prior schema versions
        for tbl in ("vdevs", "disk_groups", "disk_partitions", "nfs_exports", "datasets"):
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        conn.commit()

        # Rebuild the disks table to the current identity model.
        # device_name/device_path are ephemeral kernel names and are no longer
        # persisted; by_id is now the UNIQUE NOT NULL identity key.  Legacy rows
        # without a by_id fall back to their serial (serial used as a stable
        # surrogate when available), otherwise they are dropped.
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

        # Rebuild backup_schedules/backup_runs from the datasets-FK model to the
        # dataset_name-string model.  Datasets are now keyed purely by their ZFS
        # name (the datasets table no longer exists), so there is no surviving id
        # to backfill from; any existing schedule/run rows are dropped.
        try:
            inspector = inspect(engine)
            sched_cols = [c["name"] for c in inspector.get_columns("backup_schedules")]
            runs_cols = [c["name"] for c in inspector.get_columns("backup_runs")]
            if "dataset_id" in sched_cols or "dataset_id" in runs_cols:
                conn.execute(text("DROP TABLE IF EXISTS backup_runs"))
                conn.execute(text("DROP TABLE IF EXISTS backup_schedules"))
                conn.commit()
        except Exception:
            conn.rollback()

        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()

    # Create all tables (adds any new columns the model defines)
    from .models import pool, disk, backup, scheduler, backup_zfs
    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session for dependency injection."""
    if SessionLocal is None:
        init_db()

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context():
    """Context manager for database sessions."""
    if SessionLocal is None:
        init_db()

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
