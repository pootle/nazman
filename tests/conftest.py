import os
import tempfile
import contextlib
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nazman.database import Base, get_db, get_db_context
from nazman.main import app
from nazman.config import Settings
from nazman.auth import get_current_user


@pytest.fixture(autouse=True)
def override_settings():
    """Override settings to use temp dirs and disable auth."""
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            database_path=os.path.join(tmpdir, "test.db"),
            logging_file=os.path.join(tmpdir, "test.log"),
            backup_repo_path=os.path.join(tmpdir, "backup"),
            backup_enabled=False,
            auth_enabled=False,
            command_log_path=os.path.join(tmpdir, "command_log.db"),
        )
        with patch("nazman.config.get_settings", return_value=settings):
            with patch("nazman.database.get_settings", return_value=settings):
                yield settings


@pytest.fixture()
def db_engine(override_settings):
    """Create a fresh in-memory SQLite database for each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Create a database session for testing."""
    TestSession = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=db_engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine, override_settings):
    """Create a test client with overridden database and settings."""
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    def override_get_db_context():
        session = TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    override_get_db_context = contextlib.contextmanager(override_get_db_context)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "authenticated": True}

    with patch("nazman.database.engine", db_engine):
        with patch("nazman.database.SessionLocal", TestSession):
            with patch("nazman.database.get_db_context", override_get_db_context):
                with TestClient(app, raise_server_exceptions=False) as c:
                    yield c

    app.dependency_overrides.clear()


def mock_run_command(return_stdout="", return_stderr="", return_code=0):
    """Helper to create a mock for run_command."""
    async def _mock(cmd, timeout=300, check=True, capture_output=True):
        return (return_stdout, return_stderr, return_code)
    return _mock


def mock_run_zpool(return_stdout="", return_stderr="", return_code=0):
    """Helper to create a mock for run_zpool."""
    async def _mock(*args, **kwargs):
        return (return_stdout, return_stderr, return_code)
    return _mock


def mock_run_zfs(return_stdout="", return_stderr="", return_code=0):
    """Helper to create a mock for run_zfs."""
    async def _mock(*args, **kwargs):
        return (return_stdout, return_stderr, return_code)
    return _mock
