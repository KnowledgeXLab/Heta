from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common.tasks import get_task

router = APIRouter(prefix="/api/v1/hetawiki", tags=["hetawiki"])


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    message: str = ""
    error: str | None = None
    result: dict | None = None


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_ingest_task(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status.value,
        message=task.message,
        error=task.error,
        result=task.metadata.get("result"),
    )
