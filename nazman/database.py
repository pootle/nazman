from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base
from contextlib import contextmanager
import logging
from .config import get_settings
from .migrations import run_migrations

logger = logging.getLogger(__name__)

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
    # DDL surgery lives in nazman.migrations; run it with foreign_keys off
    # so dropping old tables doesn't fail on FK constraints.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        run_migrations(engine, conn)

        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()

    # Create all tables (adds any new columns the model defines)
    from .models import pool, disk, scheduler, backup_zfs, alert
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
