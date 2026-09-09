import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone

from nazman.managers.scheduler import scheduler_manager
from nazman.models.scheduler import ScheduledTask, TaskType


@pytest.mark.asyncio
async def test_scheduler_start_loads_all_task_types(db_session):
    """Test that scheduler.start() loads all enabled tasks from the database."""
    # Reset scheduler state
    scheduler_manager._started = False
    
    # Create tasks of different types
    scrub_task = ScheduledTask(
        name="test-scrub",
        task_type=TaskType.SCRUB.value,
        target="tank",
        schedule="0 2 * * 0",
        enabled=True
    )
    snapshot_task = ScheduledTask(
        name="test-snapshot",
        task_type=TaskType.SNAPSHOT.value,
        target="tank/data",
        schedule="0 0 * * *",
        config={"retention": 7},
        enabled=True
    )
    health_task = ScheduledTask(
        name="test-health",
        task_type=TaskType.HEALTH_CHECK.value,
        target="tank",
        schedule="*/15 * * * *",
        enabled=True
    )
    
    db_session.add_all([scrub_task, snapshot_task, health_task])
    db_session.commit()
    
    with patch('nazman.database.get_db_context') as mock_db_ctx, \
         patch.object(scheduler_manager.scheduler, 'start'), \
         patch.object(scheduler_manager.scheduler, 'add_job') as mock_add_job:
        
        # Mock the database context to return our session
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=db_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_db_ctx.return_value = mock_ctx
        
        await scheduler_manager.start()
        
        # Verify that add_job was called for each enabled task
        assert mock_add_job.call_count == 3
        
        # Verify task IDs are correct
        job_ids = [call.kwargs['id'] for call in mock_add_job.call_args_list]
        assert f"task_{scrub_task.id}" in job_ids
        assert f"task_{snapshot_task.id}" in job_ids
        assert f"task_{health_task.id}" in job_ids


@pytest.mark.asyncio
async def test_scheduler_start_skips_disabled_tasks(db_session):
    """Test that scheduler.start() skips disabled tasks."""
    disabled_task = ScheduledTask(
        name="test-disabled",
        task_type=TaskType.SCRUB.value,
        target="tank",
        schedule="0 2 * * 0",
        enabled=False
    )
    
    db_session.add(disabled_task)
    db_session.commit()
    
    with patch.object(scheduler_manager.scheduler, 'add_job') as mock_add_job:
        await scheduler_manager.start()
        
        # Verify that add_job was not called for disabled task
        assert mock_add_job.call_count == 0


@pytest.mark.asyncio
async def test_scheduler_uses_utc_timezone():
    """Test that scheduler is configured with UTC timezone."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    
    # The scheduler should be initialized with timezone=timezone.utc
    assert scheduler_manager.scheduler.timezone == timezone.utc


@pytest.mark.asyncio
async def test_execute_scrub_calls_zpool_scrub():
    """Test that _execute_scrub calls zpool scrub with correct arguments."""
    with patch('nazman.managers.scheduler.run_zpool', new_callable=AsyncMock) as mock_zpool:
        mock_zpool.return_value = ("", "", 0)
        
        await scheduler_manager._execute_scrub("tank")
        
        mock_zpool.assert_called_once_with(
            "scrub", "tank",
            timeout=3600
        )


@pytest.mark.asyncio
async def test_execute_snapshot_creates_snapshot_with_retention():
    """Test that _execute_snapshot creates a snapshot and applies retention."""
    with patch('nazman.managers.scheduler.run_zfs', new_callable=AsyncMock) as mock_zfs:
        mock_zfs.return_value = ("", "", 0)
        
        config = {"retention": 5}
        await scheduler_manager._execute_snapshot("tank/data", config)
        
        # Verify snapshot was created
        assert mock_zfs.call_count >= 1
        first_call = mock_zfs.call_args_list[0]
        assert first_call.args[0] == "snapshot"
        assert "tank/data@auto-" in first_call.args[1]


@pytest.mark.asyncio
async def test_execute_zfs_backup_uses_per_dataset_lock():
    """Test that _execute_zfs_backup uses per-dataset locking."""
    import asyncio
    
    config = {
        "dataset_name": "tank/data",
        "backup_disk_id": 1,
        "type": "full"
    }
    
    # Create a mock for the backup manager
    with patch('nazman.managers.zfs_backup_manager.zfs_backup_manager') as mock_backup, \
         patch('nazman.database.get_db_context') as mock_db_ctx:
        
        mock_backup.run_backup = AsyncMock()
        mock_db_ctx.return_value.__enter__ = MagicMock()
        mock_db_ctx.return_value.__exit__ = MagicMock()
        
        # Execute the backup
        await scheduler_manager._execute_zfs_backup(config)
        
        # Verify the lock was created for this dataset
        assert "tank/data" in scheduler_manager._dataset_locks
        
        # Verify run_backup was called
        mock_backup.run_backup.assert_called_once()


@pytest.mark.asyncio
async def test_create_task_validates_schedule(db_session):
    """Test that create_task validates the schedule format."""
    from nazman.utils.exceptions import NAZManError
    
    # Invalid schedule should raise an error
    with pytest.raises(Exception):  # ValidationError from validate_schedule
        await scheduler_manager.create_task(
            db_session,
            name="test-task",
            task_type=TaskType.SCRUB,
            target="tank",
            schedule="invalid schedule"
        )


@pytest.mark.asyncio
async def test_delete_task_removes_from_scheduler(db_session):
    """Test that delete_task removes the task from the scheduler."""
    task = ScheduledTask(
        name="test-task",
        task_type=TaskType.SCRUB.value,
        target="tank",
        schedule="0 2 * * 0",
        enabled=True
    )
    db_session.add(task)
    db_session.commit()
    
    with patch.object(scheduler_manager.scheduler, 'get_job') as mock_get_job, \
         patch.object(scheduler_manager.scheduler, 'remove_job') as mock_remove_job:
        
        mock_get_job.return_value = MagicMock()
        
        await scheduler_manager.delete_task(db_session, task.id)
        
        # Verify the job was removed from the scheduler
        mock_remove_job.assert_called_once_with(f"task_{task.id}")
        
        # Verify the task was deleted from the database
        assert db_session.query(ScheduledTask).filter(ScheduledTask.id == task.id).first() is None
