"""
In-memory task store for async job tracking.

Provides task lifecycle management with status, progress, and metadata.
Not persistent across restarts.
"""

import threading
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from common.config import load_config

_tasks_cfg = load_config("tasks")
_MAX_TASKS: int = int(_tasks_cfg.get("max_tasks", 500))
_TTL_SECONDS: int = int(_tasks_cfg.get("ttl_seconds", 3600))


class TaskStatus(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    CANCELLING = "cancelling"  # cancel requested; task is winding down
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class TaskInfo(BaseModel):
    task_id: str
    task_type: str
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    message: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

# Module-level storage
_tasks: Dict[str, TaskInfo] = {}
_cancel_tokens: Dict[str, threading.Event] = {}


def _cleanup() -> None:
    now = datetime.now()
    to_delete = [
        tid for tid, t in _tasks.items()
        if t.status in _TERMINAL
        and t.completed_at
        and (now - datetime.fromisoformat(t.completed_at)).total_seconds() > _TTL_SECONDS
    ]
    for tid in to_delete:
        del _tasks[tid]
        _cancel_tokens.pop(tid, None)

    if len(_tasks) > _MAX_TASKS:
        evictable = sorted(
            [(tid, t) for tid, t in _tasks.items() if t.status in _TERMINAL],
            key=lambda x: x[1].completed_at or x[1].created_at,
        )
        for tid, _ in evictable[: len(_tasks) - _MAX_TASKS]:
            del _tasks[tid]
            _cancel_tokens.pop(tid, None)


def create_task(task_type: str, metadata: Dict[str, Any] = None) -> TaskInfo:
    """Create a new task and return its info."""
    _cleanup()
    task_id = uuid.uuid4().hex[:12]
    task = TaskInfo(
        task_id=task_id,
        task_type=task_type,
        metadata=metadata or {},
        created_at=datetime.now().isoformat(),
    )
    _tasks[task_id] = task
    _cancel_tokens[task_id] = threading.Event()
    return task


def get_task(task_id: str) -> Optional[TaskInfo]:
    """Retrieve task by ID. Returns None if not found."""
    return _tasks.get(task_id)


def get_cancel_token(task_id: str) -> Optional[threading.Event]:
    """Return the cancellation token for a task, or None if not found."""
    return _cancel_tokens.get(task_id)


def update_task(
    task_id: str,
    status: Optional[TaskStatus] = None,
    progress: Optional[float] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update task fields. Auto-records started_at and completed_at timestamps."""
    task = _tasks.get(task_id)
    if not task:
        return
    if status is not None:
        task.status = status
        now = datetime.now().isoformat()
        if status == TaskStatus.RUNNING and not task.started_at:
            task.started_at = now
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            task.completed_at = now
    if progress is not None:
        task.progress = progress
    if message is not None:
        task.message = message
    if error is not None:
        task.error = error


def list_tasks(
    status: Optional[TaskStatus] = None,
    task_type: Optional[str] = None,
    limit: int = 50,
) -> List[TaskInfo]:
    """List tasks, optionally filtered by status or type. Newest first."""
    tasks = list(_tasks.values())
    if status is not None:
        tasks = [t for t in tasks if t.status == status]
    if task_type is not None:
        tasks = [t for t in tasks if t.task_type == task_type]
    tasks.sort(key=lambda t: t.created_at, reverse=True)
    return tasks[:limit]


def cancel_task(task_id: str) -> bool:
    """Request cancellation of a task.

    - PENDING tasks are cancelled immediately.
    - RUNNING tasks are moved to CANCELLING and their cancel token is set;
      the task itself is responsible for checking the token and cleaning up.

    Returns True if cancellation was accepted, False if the task cannot be
    cancelled (not found, already in a terminal state, or already cancelling).
    """
    task = _tasks.get(task_id)
    if not task:
        return False
    now = datetime.now().isoformat()
    token = _cancel_tokens.get(task_id)
    if task.status == TaskStatus.PENDING:
        task.status = TaskStatus.CANCELLED
        task.completed_at = now
        # Set the token so run_file_processing exits immediately if the task
        # has already been handed to the thread pool but not started yet.
        if token:
            token.set()
        return True
    if task.status == TaskStatus.RUNNING:
        task.status = TaskStatus.CANCELLING
        if token:
            token.set()
        return True
    return False
