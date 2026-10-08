from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..services.task_status import TaskStatusService
from ..wiring import get_task_status_service

router = APIRouter(
    prefix="/api/tasks",
    tags=["tasks"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/active", response_model=dict)
async def active_tasks(
    db: Session = Depends(get_db),
    service: TaskStatusService = Depends(get_task_status_service),
):
    """Long-running tasks currently in progress across all domains."""
    tasks = await service.active_tasks(db)
    return {"tasks": tasks, "count": len(tasks)}