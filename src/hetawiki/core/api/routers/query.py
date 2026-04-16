from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from pydantic import BaseModel

from common.config import load_config
from common.tasks import TaskStatus, create_task, get_task, update_task
from hetawiki.core.wiki.query import run_query

router = APIRouter(prefix="/api/v1/hetawiki", tags=["hetawiki"])
logger = logging.getLogger(__name__)

_cfg = load_config("hetawiki")
_executor = ThreadPoolExecutor(
    max_workers=_cfg.get("ingest", {}).get("max_workers", 4),
    thread_name_prefix="hetawiki-query",
)


class QueryRequest(BaseModel):
    question: str


class QueryResponse(BaseModel):
    task_id: str
    status: str


@router.post("/query", response_model=QueryResponse, status_code=202)
async def query(request: QueryRequest):
    task = create_task("hetawiki_query", metadata={"question": request.question})
    _executor.submit(_run_query, task.task_id, request.question)
    return QueryResponse(task_id=task.task_id, status="queued")


def _run_query(task_id: str, question: str) -> None:
    update_task(task_id, status=TaskStatus.RUNNING, message="running query")
    try:
        result = run_query(question, task_id)
        update_task(task_id, status=TaskStatus.COMPLETED, message="done", progress=1.0)
        task = get_task(task_id)
        if task is not None:
            task.metadata["result"] = result
    except Exception as exc:
        logger.error("query failed: %s", exc, exc_info=True)
        update_task(task_id, status=TaskStatus.FAILED, error=str(exc))
