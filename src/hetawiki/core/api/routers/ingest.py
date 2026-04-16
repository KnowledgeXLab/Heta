from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from common.config import load_config
from common.tasks import TaskStatus, create_task, get_task, update_task
from hetawiki.config import load_wiki_config
from hetawiki.core.wiki.default_ingest import run_default_ingest
from hetawiki.core.wiki.merge_ingest import run_merge_ingest
from hetawiki.core.wiki.parser import parse_to_markdown
from hetawiki.core.wiki.store import save_raw_upload

router = APIRouter(prefix="/api/v1/hetawiki", tags=["hetawiki"])
logger = logging.getLogger(__name__)

_cfg = load_config("hetawiki")
_executor = ThreadPoolExecutor(
    max_workers=_cfg.get("ingest", {}).get("max_workers", 4),
    thread_name_prefix="hetawiki-ingest",
)
_MAX_INPUT_CHARS = int(_cfg.get("ingest", {}).get("max_input_chars", 80000))

_SUPPORTED_EXTENSIONS = load_wiki_config().supported_extensions


class IngestResponse(BaseModel):
    task_id: str
    status: str
    filename: str


@router.post("/ingest", response_model=IngestResponse, status_code=202)
async def ingest(
    file: UploadFile = File(...),
    merge: bool = Form(False),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must include a filename")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: '{suffix}'. Supported: {sorted(_SUPPORTED_EXTENSIONS)}",
        )

    raw_path = save_raw_upload(file.filename, file.file)
    task = create_task(
        "hetawiki_ingest",
        metadata={"source_path": str(raw_path), "merge": merge, "filename": file.filename},
    )
    _executor.submit(_run_ingest, task.task_id, str(raw_path), merge)
    return IngestResponse(task_id=task.task_id, status="queued", filename=raw_path.name)


def _run_ingest(task_id: str, source_path: str, merge: bool) -> None:
    update_task(task_id, status=TaskStatus.RUNNING, message="starting ingest")
    try:
        raw_path = Path(source_path)
        if not raw_path.exists():
            raise FileNotFoundError(f"source not found: {source_path}")

        update_task(task_id, message="parsing document", progress=0.3)
        markdown = parse_to_markdown(raw_path)
        if not merge and len(markdown) > _MAX_INPUT_CHARS:
            raise ValueError(
                f"Document too long for default ingest: {len(markdown)} chars > {_MAX_INPUT_CHARS}"
            )

        task = get_task(task_id)
        if merge:
            update_task(task_id, message="running merge ingest", progress=0.7)
            result = run_merge_ingest(markdown, raw_path, task_id)
        else:
            update_task(task_id, message="writing wiki page", progress=0.7)
            result = run_default_ingest(markdown, raw_path)

        if task is not None:
            # Normalise so both default and merge paths expose written_paths list
            written = result.get("written_paths") or (
                [result["page_path"]] if result.get("page_path") else []
            )
            task.metadata["result"] = {
                "source_path": str(raw_path),
                "merge": merge,
                "written_paths": written,
                "title": result.get("title", ""),
                **result,
            }

        update_task(task_id, status=TaskStatus.COMPLETED, message="ingested", progress=1.0)
    except Exception as exc:
        logger.error("ingest failed: %s", exc, exc_info=True)
        update_task(task_id, status=TaskStatus.FAILED, error=str(exc))
