"""Aggregate long-running work into one view for the global activity indicator.

Backups, system restores and scheduled tasks (scrubs, etc.) each track their
own state in different places; this service composes them into a single list
of currently-running tasks so any screen can show what the NAS is busy with.
Cross-domain like the other services, so it reaches into the injected managers
rather than duplicating their queries.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TaskStatusService:
    def __init__(self, backup_groups=None, system_restore=None, scheduler=None):
        self.backup_groups = backup_groups
        self.system_restore = system_restore
        self.scheduler = scheduler

    async def active_tasks(self, db: Session) -> list[dict]:
        """Every currently-running long task, newest-first within each kind."""
        tasks: list[dict] = []
        tasks.extend(await self._backups(db))
        tasks.extend(self._restores())
        tasks.extend(await self._scheduled(db))
        return tasks

    async def _backups(self, db: Session) -> list[dict]:
        if self.backup_groups is None:
            return []
        try:
            sessions = await self.backup_groups.list_sessions(db, limit=50)
        except Exception:
            logger.warning("could not list backup sessions", exc_info=True)
            return []
        out = []
        for s in sessions:
            if s.get("status") != "running":
                continue
            out.append({
                "kind": "backup",
                "id": s.get("id"),
                "label": s.get("group_name") or "Backup",
                "detail": s.get("current_dataset"),
                "progress_pct": s.get("progress_pct"),
                "started_at": s.get("started_at"),
                "link": "/backup",
            })
        return out

    def _restores(self) -> list[dict]:
        if self.system_restore is None:
            return []
        try:
            return self.system_restore.active_jobs()
        except Exception:
            logger.warning("could not read restore job", exc_info=True)
            return []

    async def _scheduled(self, db: Session) -> list[dict]:
        from ..models.scheduler import ScheduledTask, TaskHistory

        try:
            rows = (
                db.query(TaskHistory, ScheduledTask)
                .join(ScheduledTask, ScheduledTask.id == TaskHistory.task_id)
                .filter(TaskHistory.status == "running")
                .order_by(TaskHistory.id.desc())
                .all()
            )
        except Exception:
            logger.warning("could not list scheduled tasks", exc_info=True)
            return []
        return [
            {
                "kind": "scheduled",
                "id": task.id,
                "label": (task.task_type or "task").replace("_", " ").title(),
                "detail": task.name or task.config,
                "progress_pct": None,
                "started_at": history.started_at,
                "link": "/settings",
            }
            for history, task in rows
        ]