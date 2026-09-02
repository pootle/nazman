import pytest
from unittest.mock import patch, AsyncMock
from nazman.models.backup import BackupCommit


@pytest.mark.asyncio
async def test_get_backup_status(client):
    with patch("nazman.api.backup.backup_manager") as mock:
        mock.get_backup_status = AsyncMock(return_value={
            "repo_exists": False,
            "repo_path": "/tmp/backup",
            "last_commit": None,
            "has_uncommitted_changes": False,
            "backup_enabled": True,
        })
        response = client.get("/api/backup/status")
        assert response.status_code == 200
        data = response.json()
        assert "repo_exists" in data
        assert "backup_enabled" in data


@pytest.mark.asyncio
async def test_get_backup_history_empty(client):
    with patch("nazman.api.backup.backup_manager") as mock:
        mock.get_backup_history = AsyncMock(return_value=[])
        response = client.get("/api/backup/history")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_get_backup_history(client, db_session):
    commit = BackupCommit(
        commit_hash="abc123",
        commit_message="Test backup",
        author="nazman",
        files_changed=3,
    )
    db_session.add(commit)
    db_session.commit()

    with patch("nazman.api.backup.backup_manager") as mock:
        mock.get_backup_history = AsyncMock(return_value=[commit])
        response = client.get("/api/backup/history")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["commit_hash"] == "abc123"


@pytest.mark.asyncio
async def test_create_backup(client, db_session):
    with patch("nazman.api.backup.backup_manager") as mock:
        async def fake_backup(db, message=None):
            commit = BackupCommit(
                commit_hash="def456",
                commit_message=message or "Auto backup",
                author="nazman",
                files_changed=5,
            )
            db_session.add(commit)
            db_session.commit()
            db_session.refresh(commit)
            return commit

        mock.backup_configuration = AsyncMock(side_effect=fake_backup)
        response = client.post("/api/backup/backup", params={
            "message": "Manual backup",
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["commit_hash"] == "def456"
        assert data["commit_message"] == "Manual backup"


@pytest.mark.asyncio
async def test_restore_backup(client):
    with patch("nazman.api.backup.backup_manager") as mock:
        mock.restore_configuration = AsyncMock(return_value=True)
        response = client.post("/api/backup/restore", json={
            "commit_hash": "abc123",
        })
        assert response.status_code == 200
        assert "restored" in response.json()["message"]


@pytest.mark.asyncio
async def test_restore_backup_failure(client):
    with patch("nazman.api.backup.backup_manager") as mock:
        mock.restore_configuration = AsyncMock(return_value=False)
        response = client.post("/api/backup/restore", json={
            "commit_hash": "nonexistent",
        })
        assert response.status_code == 200
        assert "failed" in response.json()["message"]
