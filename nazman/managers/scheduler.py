from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..models.scheduler import ScheduledTask, TaskHistory, TaskType
from ..utils.commands import run_zpool, run_zfs
from ..utils.validation import validate_schedule
from ..utils.exceptions import NAZManError


class SchedulerManager:
    """Manages scheduled tasks for scrubs, snapshots, etc."""
    
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._started = False
    
    async def start(self):
        """Start the scheduler."""
        if not self._started:
            self.scheduler.start()
            self._started = True
            # Reload persistent schedule-driven jobs (e.g. ZFS backups) that
            # are derived from backup_schedules rows.
            from ..database import get_db_context
            from .zfs_backup_manager import zfs_backup_manager
            with get_db_context() as db:
                await zfs_backup_manager.sync_scheduled_tasks(db)
    
    async def stop(self):
        """Stop the scheduler."""
        if self._started:
            self.scheduler.shutdown()
            self._started = False
    
    async def list_tasks(self, db: Session) -> List[ScheduledTask]:
        """List all scheduled tasks."""
        return db.query(ScheduledTask).all()
    
    async def create_task(
        self,
        db: Session,
        name: str,
        task_type: TaskType,
        target: str,
        schedule: str,
        config: Optional[Dict[str, Any]] = None
    ) -> ScheduledTask:
        """Create a new scheduled task."""
        # Validate schedule
        validate_schedule(schedule)
        
        # Create database record
        task = ScheduledTask(
            name=name,
            task_type=task_type.value,
            target=target,
            schedule=schedule,
            config=config,
            enabled=True
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        
        # Add to scheduler
        await self._schedule_task(task)
        
        return task
    
    async def update_task(
        self,
        db: Session,
        task_id: int,
        name: Optional[str] = None,
        schedule: Optional[str] = None,
        enabled: Optional[bool] = None,
        config: Optional[Dict[str, Any]] = None
    ) -> ScheduledTask:
        """Update a scheduled task."""
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            raise NAZManError(f"Task with id {task_id} not found")
        
        if name is not None:
            task.name = name
        if schedule is not None:
            validate_schedule(schedule)
            task.schedule = schedule
        if enabled is not None:
            task.enabled = enabled
        if config is not None:
            task.config = config
        
        task.updated_at = datetime.now(timezone.utc)
        
        # Reschedule task
        await self._unschedule_task(task)
        if task.enabled:
            await self._schedule_task(task)
        
        db.commit()
        db.refresh(task)
        return task
    
    async def delete_task(self, db: Session, task_id: int) -> None:
        """Delete a scheduled task."""
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            raise NAZManError(f"Task with id {task_id} not found")
        
        # Remove from scheduler
        await self._unschedule_task(task)
        
        # Delete from database
        db.delete(task)
        db.commit()
    
    async def run_task_now(self, task_id: int) -> None:
        """Run a task immediately."""
        # This would trigger the task execution
        # For now, just log that it was triggered
        print(f"Task {task_id} triggered manually")
    
    async def get_task_history(self, db: Session, task_id: Optional[int] = None) -> List[TaskHistory]:
        """Get task execution history."""
        query = db.query(TaskHistory)
        if task_id:
            query = query.filter(TaskHistory.task_id == task_id)
        return query.order_by(TaskHistory.started_at.desc()).limit(100).all()
    
    async def _schedule_task(self, task: ScheduledTask) -> None:
        """Schedule a task with APScheduler."""
        try:
            # Parse cron schedule
            parts = task.schedule.split()
            if len(parts) != 5:
                return
            
            minute, hour, day, month, day_of_week = parts
            
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week
            )
            
            # Add job to scheduler
            self.scheduler.add_job(
                self._execute_task,
                trigger=trigger,
                args=[task.id],
                id=f"task_{task.id}",
                name=task.name,
                replace_existing=True
            )
            
        except Exception as e:
            print(f"Failed to schedule task {task.id}: {str(e)}")
    
    async def _unschedule_task(self, task: ScheduledTask) -> None:
        """Remove a task from the scheduler."""
        try:
            job_id = f"task_{task.id}"
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        except Exception as e:
            print(f"Failed to unschedule task {task.id}: {str(e)}")
    
    async def _execute_task(self, task_id: int) -> None:
        """Execute a scheduled task."""
        from ..database import get_db_context
        
        with get_db_context() as db:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task:
                return
            
            # Create history record
            history = TaskHistory(
                task_id=task.id,
                started_at=datetime.now(timezone.utc),
                status="running"
            )
            db.add(history)
            db.commit()
            
            try:
                # Execute based on task type
                if task.task_type == TaskType.SCRUB.value:
                    await self._execute_scrub(task.target)
                elif task.task_type == TaskType.SNAPSHOT.value:
                    await self._execute_snapshot(task.target, task.config)
                elif task.task_type == TaskType.BACKUP.value:
                    await self._execute_backup()
                elif task.task_type == TaskType.ZFS_BACKUP.value:
                    await self._execute_zfs_backup(task.config)
                elif task.task_type == TaskType.HEALTH_CHECK.value:
                    await self._execute_health_check(task.target)
                
                # Update history
                history.status = "success"
                history.completed_at = datetime.now(timezone.utc)
                
                # Update task last run
                task.last_run = datetime.now(timezone.utc)
                
            except Exception as e:
                history.status = "failed"
                history.error = str(e)
                history.completed_at = datetime.now(timezone.utc)
            
            db.commit()
    
    async def _execute_scrub(self, pool_name: str) -> None:
        """Execute a scrub on a pool."""
        stdout, stderr, returncode = await run_zpool(
            "scrub", pool_name,
            timeout=3600
        )
        
        if returncode != 0:
            raise NAZManError(f"Scrub failed: {stderr}")
    
    async def _execute_snapshot(self, dataset_name: str, config: Dict[str, Any]) -> None:
        """Execute a snapshot."""
        snapshot_name = f"auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        full_snapshot = f"{dataset_name}@{snapshot_name}"
        
        stdout, stderr, returncode = await run_zfs(
            "snapshot", full_snapshot,
            timeout=300
        )
        
        if returncode != 0:
            raise NAZManError(f"Snapshot failed: {stderr}")
        
        # Apply retention policy if configured
        if config and "retention" in config:
            await self._apply_retention(dataset_name, config["retention"])
    
    async def _apply_retention(self, dataset_name: str, retention: int) -> None:
        """Apply snapshot retention policy."""
        # List snapshots for dataset
        stdout, stderr, returncode = await run_zfs(
            "list", "-H", "-o", "name", "-t", "snapshot", "-r", dataset_name, op="read",
        )
        
        if returncode == 0:
            snapshots = [s for s in stdout.strip().split('\n') if s]
            
            # Sort by creation time (newest first)
            snapshots.sort(reverse=True)
            
            # Delete old snapshots
            for snapshot in snapshots[retention:]:
                await run_zfs("destroy", snapshot, timeout=60)
    
    async def _execute_backup(self) -> None:
        """Execute a backup."""
        from .backup_manager import backup_manager
        from ..database import get_db_context

        with get_db_context() as db:
            await backup_manager.backup_configuration(db)

    async def _execute_zfs_backup(self, config: Dict[str, Any]) -> None:
        """Execute a ZFS data backup (dataset -> backup disk)."""
        from .zfs_backup_manager import zfs_backup_manager
        from ..database import get_db_context

        config = config or {}
        dataset_name = config.get("dataset_name")
        backup_disk_id = config.get("backup_disk_id")
        backup_type = config.get("type", "full") or "full"
        if not dataset_name or not backup_disk_id:
            raise NAZManError("ZFS backup task requires dataset_name and backup_disk_id")

        with get_db_context() as db:
            await zfs_backup_manager.run_backup(
                db,
                dataset_name=dataset_name,
                backup_disk_id=backup_disk_id,
                backup_type=backup_type,
            )

    
    async def _execute_health_check(self, target: str) -> None:
        """Execute a health check."""
        # Check pool status
        stdout, stderr, returncode = await run_zpool(
            "status", "-j", target, op="read",
        )
        
        if returncode != 0:
            raise NAZManError(f"Health check failed: {stderr}")
        
        # Parse status and check for errors
        import json
        data = json.loads(stdout)
        pool_info = data.get("pools", [{}])[0] if data.get("pools") else {}
        
        if pool_info.get("state") != "ONLINE":
            raise NAZManError(f"Pool {target} is not ONLINE: {pool_info.get('state')}")


# Singleton instance
scheduler_manager = SchedulerManager()
