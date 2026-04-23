"""Knowledge base and raw file management API.

Storage layout (workspace root configured via hetadb.workspace in config.yaml):

    workspace/
      raw_files/{dataset}/          ← source documents, managed here
      kb/{kb_name}/
        _meta.json                  ← KB metadata (created_at)
        {dataset}/
          _meta.json                ← process_mode, parsed_at
          parsed_file/
          kg_file/

Endpoint groups:

  Raw files  — GET/POST /raw-files/datasets
               GET/POST/DELETE /raw-files/datasets/{dataset}/files
               DELETE /raw-files/datasets/{dataset}

  Knowledge bases — GET/POST /knowledge-bases
                    GET/DELETE /knowledge-bases/{kb_name}
                    DELETE /knowledge-bases/{kb_name}/datasets/{dataset_name}
                    POST /knowledge-bases/{kb_name}/parse

  Tasks      — GET /processing/tasks[/{task_id}]
               POST /processing/tasks/{task_id}/cancel
               GET /processing/config
"""

import json
import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

from common.tasks import TaskStatus, create_task, get_task, get_cancel_token, list_tasks, cancel_task
from hetadb.core.file_processor import ConfigManager, run_file_processing
from hetadb.utils.file_url import (
    delete_dataset_from_s3,
    delete_file_from_s3,
    s3_configured,
    upload_file_to_s3,
)
from hetadb.core.kb_profile.overview import generate_kb_overview, format_for_prompt
from hetadb.core.mode_registry import get_query_modes

router = APIRouter(prefix="/api/v1/hetadb/files", tags=["files"])
logger = logging.getLogger(__name__)

# Dedicated thread pool for parse tasks, isolated from FastAPI's default pool
# so that long-running parsing never starves regular API requests.
# Size is read from db_config.yaml (parse_max_workers, default 2).
_parse_executor: ThreadPoolExecutor | None = None


def _get_parse_executor() -> ThreadPoolExecutor:
    global _parse_executor
    if _parse_executor is None:
        max_workers = ConfigManager().get_parse_max_workers()
        _parse_executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="hetadb-parse",
        )
        logger.info("Parse executor initialised with max_workers=%d", max_workers)
    return _parse_executor


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

_workspace_root: Path | None = None


def _get_workspace_root() -> Path:
    """Return the resolved workspace root (cached after first call)."""
    global _workspace_root
    if _workspace_root is None:
        _workspace_root = ConfigManager().get_workspace_root()
    return _workspace_root


def _raw_files_dir() -> Path:
    return _get_workspace_root() / "raw_files"


def _kb_dir() -> Path:
    return _get_workspace_root() / "kb"


def _read_meta(path: Path) -> dict:
    """Read a _meta.json file; return empty dict if missing or corrupted."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read meta file %s: %s", path, e)
        return {}


def _write_meta(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# File upload helpers
# ---------------------------------------------------------------------------

def _safe_filename(raw: str | None) -> str:
    """Strip path components to prevent path-traversal attacks."""
    name = PurePosixPath(raw or "unknown").name
    return name or "unknown"


def _unique_path(directory: Path, filename: str) -> tuple[str, Path]:
    """Return a (filename, path) pair that does not collide with existing files."""
    dest = directory / filename
    if dest.exists():
        stem, suffix = Path(filename).stem, Path(filename).suffix
        filename = f"{stem}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{suffix}"
        dest = directory / filename
    return filename, dest


async def _stream_upload(file: UploadFile, dest: Path) -> int:
    """Stream an uploaded file to disk in 1 MB chunks. Returns bytes written."""
    total = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
                total += len(chunk)
    except OSError as e:
        # Clean up partial file before re-raising
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}")
    return total


# ---------------------------------------------------------------------------
# DB cleanup helper
# ---------------------------------------------------------------------------

def _purge_kb_db(kb_name: str, datasets: list[str], kb_path: Path | None = None) -> None:
    """Drop Milvus collections and PG tables for the given datasets in a KB.

    kb_path is used to discover CSV-derived PostgreSQL table names from each
    dataset's table_info/ directory.  Errors are logged as warnings so that a
    partial DB failure never blocks the filesystem deletion that follows.
    """
    try:
        from pymilvus import utility
        from hetadb.core.db_build.vector_db.vector_db import connect_milvus
        from hetadb.core.db_build.sql_db.sql_db import drop_dataset_tables
        from hetadb.utils.load_config import get_postgres_conn_config
        import psycopg2

        connect_milvus()
        # Keep this list in sync with _clean_dataset in file_processor.py.
        suffixes = (
            "_chunk_collection",
            "_merge_chunk_collection",
            "_entity_collection",
            "_relation_collection",
            "_node_dedup_collection",
            "_rel_dedup_collection",
        )
        for ds in datasets:
            prefix = f"{kb_name}__{ds}"

            # Drop Milvus vector collections.
            for suffix in suffixes:
                name = f"{prefix}{suffix}"
                if utility.has_collection(name):
                    utility.drop_collection(name)
                    logger.info("Dropped Milvus collection: %s", name)

            # Drop standard PostgreSQL tables (chunks, entities, relations, …).
            try:
                drop_dataset_tables(prefix)
            except Exception as e:
                logger.warning("Failed to drop PG tables for %s: %s", prefix, e)

            # Drop CSV-derived PostgreSQL tables whose names are recorded in
            # table_info/*.json (each file's stem is the table name).
            if kb_path is not None:
                table_info_dir = kb_path / ds / "parsed_file" / "table_info"
                csv_tables = [p.stem for p in table_info_dir.glob("*.json")] if table_info_dir.exists() else []
                if csv_tables:
                    try:
                        conn = psycopg2.connect(**get_postgres_conn_config())
                        try:
                            with conn.cursor() as cur:
                                for tbl in csv_tables:
                                    cur.execute(f'DROP TABLE IF EXISTS public."{tbl}" CASCADE')
                                    logger.info("Dropped CSV-derived PG table: %s", tbl)
                            conn.commit()
                        finally:
                            conn.close()
                    except Exception as e:
                        logger.warning("Failed to drop CSV-derived PG tables for %s/%s: %s", kb_name, ds, e)
    except Exception as e:
        logger.warning("DB purge failed for KB '%s': %s", kb_name, e)


_UPLOAD_SESSION_TTL = 24 * 3600  # seconds


def cleanup_stale_upload_sessions() -> None:
    """Remove chunked upload sessions that have not been completed within TTL.

    Called at application startup so that orphaned sessions left by crashed
    clients do not accumulate on disk indefinitely.
    """
    now = time.time()
    raw = _raw_files_dir()
    if not raw.exists():
        return
    for uploads_dir in raw.glob("*/.uploads"):
        for session in uploads_dir.iterdir():
            if not session.is_dir():
                continue
            meta = _read_meta(session / "_meta.json")
            age = now - meta.get("created_at", 0)
            if age > _UPLOAD_SESSION_TTL:
                try:
                    shutil.rmtree(session)
                    logger.info("Removed stale upload session %s (age=%.0fh)", session.name, age / 3600)
                except OSError as e:
                    logger.warning("Failed to remove stale upload session %s: %s", session.name, e)


def _run_kb_deletion(kb_name: str, kb_path: Path, purge_db: bool, task_ids: list[str]) -> None:
    """Wait for active tasks to reach a terminal state, then purge DB and filesystem."""
    deadline = time.time() + 600  # 10-minute safety timeout
    while time.time() < deadline:
        pending = [
            tid for tid in task_ids
            if (t := get_task(tid)) is not None
            and t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        if not pending:
            break
        time.sleep(2)
    else:
        logger.warning("KB '%s' deletion: timed out waiting for tasks; proceeding anyway", kb_name)

    if purge_db and kb_path.exists():
        datasets = [p.name for p in kb_path.iterdir() if p.is_dir()]
        _purge_kb_db(kb_name, datasets, kb_path=kb_path)

    try:
        shutil.rmtree(kb_path)
    except OSError as e:
        logger.error("Failed to delete KB '%s' directory: %s", kb_name, e)
    else:
        logger.info("KB '%s' deleted (purge_db=%s)", kb_name, purge_db)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_NAME_HINT = "Only letters, digits and underscores are allowed."


def _validate_safe_name(name: str) -> str:
    """Reject names that could break filesystem paths or DB identifiers."""
    if not _NAME_RE.match(name):
        raise ValueError(f"Invalid name '{name}'. {_NAME_HINT}")
    return name


class NameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_safe_name(v)


class KBCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_safe_name(v)


class ParseRequest(BaseModel):
    datasets: list[str] = Field(..., min_length=1, description="Datasets from raw_files to parse")
    mode: int = Field(default=0, description="Processing pipeline mode")
    schema_name: str | None = Field(default=None, description="Custom entity schema name (from workspace/schemas/)")
    force: bool = Field(
        default=False,
        description=(
            "Allow overwriting already-parsed datasets. When false (default), "
            "the request is rejected with HTTP 409 if any requested dataset has "
            "been parsed before, so callers can prompt users for confirmation."
        ),
    )

    @field_validator("datasets")
    @classmethod
    def validate_dataset_names(cls, v: list[str]) -> list[str]:
        for name in v:
            _validate_safe_name(name)
        return v


class ApiResponse(BaseModel):
    success: bool
    message: str
    data: Any = None


class InitUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    total_chunks: int = Field(..., ge=1, le=10_000)
    total_size: int = Field(..., ge=1)


# ---------------------------------------------------------------------------
# Chunked upload helpers
# ---------------------------------------------------------------------------

_UPLOAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _uploads_dir(dataset: str) -> Path:
    """Temporary directory for in-progress chunked upload sessions."""
    return _raw_files_dir() / dataset / ".uploads"


def _session_dir(dataset: str, upload_id: str) -> Path:
    return _uploads_dir(dataset) / upload_id


def _validate_upload_id(upload_id: str) -> None:
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload_id")


# ---------------------------------------------------------------------------
# Raw files endpoints
# ---------------------------------------------------------------------------

@router.get("/raw-files/datasets")
async def list_raw_datasets():
    """List all datasets in raw_files."""
    base = _raw_files_dir()
    if not base.exists():
        return {"success": True, "data": []}
    datasets = sorted(d.name for d in base.iterdir() if d.is_dir())
    return {"success": True, "data": datasets}


@router.post("/raw-files/datasets", response_model=ApiResponse)
async def create_raw_dataset(body: NameRequest):
    """Create an empty dataset directory in raw_files."""
    path = _raw_files_dir() / body.name
    if path.exists():
        raise HTTPException(status_code=409, detail=f"Dataset '{body.name}' already exists")
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"Dataset '{body.name}' already exists")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to create dataset: {e}")
    return ApiResponse(success=True, message=f"Dataset '{body.name}' created")


@router.get("/raw-files/datasets/{dataset}/files")
async def list_raw_files(dataset: str):
    """List files in a raw_files dataset."""
    base = _raw_files_dir() / dataset
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset}' not found")
    files = []
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        # Skip files inside hidden directories (e.g., .uploads temp sessions)
        if any(part.startswith(".") for part in f.relative_to(base).parts):
            continue
        stat = f.stat()
        files.append({
            "name": str(f.relative_to(base)),
            "size": stat.st_size,
            "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return {"success": True, "dataset": dataset, "files": files}


@router.post("/raw-files/datasets/{dataset}/files")
async def upload_raw_files(dataset: str, files: list[UploadFile] = File(...)):
    """Upload one or more files via multipart form (simple upload, for dev/testing).

    For production use prefer the chunked upload endpoints which support large
    files and progress tracking.
    """
    base = _raw_files_dir() / dataset
    base.mkdir(parents=True, exist_ok=True)
    uploaded = []
    for file in files:
        filename, dest = _unique_path(base, _safe_filename(file.filename))
        size = await _stream_upload(file, dest)
        if s3_configured():
            try:
                upload_file_to_s3(dataset, filename, str(dest))
            except Exception as e:
                dest.unlink(missing_ok=True)
                logger.error("S3 upload failed for %s/%s: %s", dataset, filename, e)
                raise HTTPException(status_code=500, detail=f"S3 upload failed: {e}")
        logger.info("Uploaded %s to raw_files/%s (%d bytes)", filename, dataset, size)
        uploaded.append({"filename": filename, "size": size})
    return {"success": True, "message": f"{len(uploaded)} file(s) uploaded", "dataset": dataset, "files": uploaded}


@router.post("/raw-files/datasets/{dataset}/file", response_model=ApiResponse)
async def upload_raw_file(dataset: str, file: UploadFile = File(...)):
    """Upload a single file via multipart form (simple upload, for dev/testing).

    For production use prefer the chunked upload endpoints which support large
    files and progress tracking.
    """
    base = _raw_files_dir() / dataset
    base.mkdir(parents=True, exist_ok=True)
    filename, dest = _unique_path(base, _safe_filename(file.filename))
    size = await _stream_upload(file, dest)
    if s3_configured():
        try:
            upload_file_to_s3(dataset, filename, str(dest))
        except Exception as e:
            dest.unlink(missing_ok=True)
            logger.error("S3 upload failed for %s/%s: %s", dataset, filename, e)
            raise HTTPException(status_code=500, detail=f"S3 upload failed: {e}")
    logger.info("Uploaded %s to raw_files/%s (%d bytes)", filename, dataset, size)
    return ApiResponse(success=True, message="File uploaded", data={"filename": filename, "size": size})


@router.post("/raw-files/datasets/{dataset}/upload/init")
async def init_chunked_upload(dataset: str, body: InitUploadRequest):
    """Initialize a chunked upload session.

    Returns an ``upload_id`` (UUID) that must be passed to subsequent chunk
    and complete/abort calls.
    """
    base = _raw_files_dir() / dataset
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset}' not found")

    upload_id = str(uuid.uuid4())
    session = _session_dir(dataset, upload_id)
    session.mkdir(parents=True)
    (session / "_meta.json").write_text(
        json.dumps({
            "filename": body.filename,
            "total_chunks": body.total_chunks,
            "total_size": body.total_size,
            "created_at": time.time(),
        }),
        encoding="utf-8",
    )
    logger.info(
        "Chunked upload init: dataset=%s filename=%s chunks=%d id=%s",
        dataset, body.filename, body.total_chunks, upload_id,
    )
    return {"upload_id": upload_id}


@router.post("/raw-files/datasets/{dataset}/upload/{upload_id}/chunk")
async def upload_chunk(
    dataset: str, upload_id: str, chunk_index: int, request: Request,
):
    """Upload a single raw-binary chunk. ``chunk_index`` is zero-based."""
    _validate_upload_id(upload_id)
    session = _session_dir(dataset, upload_id)
    if not session.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    try:
        meta = json.loads((session / "_meta.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read session metadata: {e}")

    total_chunks = meta["total_chunks"]
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_index must be in [0, {total_chunks})",
        )

    chunk_path = session / f"{chunk_index:05d}.part"
    try:
        written = 0
        with open(chunk_path, "wb") as f:
            async for piece in request.stream():
                f.write(piece)
                written += len(piece)
        if written == 0:
            chunk_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty chunk body")
    except HTTPException:
        raise
    except Exception as e:
        chunk_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to write chunk: {e}")

    return {"received": chunk_index}


@router.post("/raw-files/datasets/{dataset}/upload/{upload_id}/complete", response_model=ApiResponse)
async def complete_chunked_upload(dataset: str, upload_id: str):
    """Merge all received chunks into the final file and clean up the session."""
    _validate_upload_id(upload_id)
    session = _session_dir(dataset, upload_id)
    if not session.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    try:
        meta = json.loads((session / "_meta.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read session metadata: {e}")

    total_chunks: int = meta["total_chunks"]
    for i in range(total_chunks):
        if not (session / f"{i:05d}.part").exists():
            raise HTTPException(status_code=400, detail=f"Missing chunk {i}")

    base = _raw_files_dir() / dataset
    filename, dest = _unique_path(base, _safe_filename(meta["filename"]))

    total = 0
    try:
        with open(dest, "wb") as out:
            for i in range(total_chunks):
                chunk_path = session / f"{i:05d}.part"
                total += chunk_path.stat().st_size
                with open(chunk_path, "rb") as cf:
                    shutil.copyfileobj(cf, out)
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to merge chunks: {e}")

    try:
        shutil.rmtree(session)
    except OSError as e:
        logger.warning("Failed to clean up upload session %s: %s", upload_id, e)

    if s3_configured():
        try:
            upload_file_to_s3(dataset, filename, str(dest))
        except Exception as e:
            dest.unlink(missing_ok=True)
            logger.error("S3 upload failed for %s/%s: %s", dataset, filename, e)
            raise HTTPException(status_code=500, detail=f"S3 upload failed: {e}")

    logger.info(
        "Chunked upload complete: dataset=%s filename=%s size=%d id=%s",
        dataset, filename, total, upload_id,
    )
    return ApiResponse(success=True, message="Upload complete", data={"filename": filename, "size": total})


@router.delete("/raw-files/datasets/{dataset}/upload/{upload_id}", response_model=ApiResponse)
async def abort_chunked_upload(dataset: str, upload_id: str):
    """Abort an upload session and delete all temp chunk files."""
    _validate_upload_id(upload_id)
    session = _session_dir(dataset, upload_id)
    if not session.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")
    try:
        shutil.rmtree(session)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to clean up session: {e}")
    logger.info("Chunked upload aborted: id=%s", upload_id)
    return ApiResponse(success=True, message="Upload aborted")


@router.delete("/raw-files/datasets/{dataset}/files/{filename:path}", response_model=ApiResponse)
async def delete_raw_file(dataset: str, filename: str):
    """Delete a file from a raw_files dataset.

    Removes the local copy first, then issues a best-effort S3 delete.  If the
    S3 call fails the local deletion is *not* rolled back — the file is gone
    from the primary store and the S3 object will become an orphan, which is
    preferable to leaving a stale local file.  The S3 error is logged as a
    warning so operators can act on it.
    """
    path = _raw_files_dir() / dataset / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File '{filename}' not found")
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")

    try:
        delete_file_from_s3(dataset, filename)
    except Exception:
        logger.warning(
            "Local file deleted but S3 removal failed for %s/%s", dataset, filename,
            exc_info=True,
        )

    logger.info("Deleted raw_files/%s/%s", dataset, filename)
    return ApiResponse(success=True, message=f"File '{filename}' deleted")


@router.delete("/raw-files/datasets/{dataset}", response_model=ApiResponse)
async def delete_raw_dataset(dataset: str):
    """Delete a raw_files dataset and all its files.

    Removes the local directory first, then bulk-deletes all objects under the
    ``{dataset}/`` prefix in S3 using paginated ListObjectsV2 + DeleteObjects.
    S3 errors are logged as warnings and do not fail the request.
    """
    path = _raw_files_dir() / dataset
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset}' not found")

    active = [
        t for t in list_tasks(limit=1000)
        if t.metadata.get("dataset") == dataset
        and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.CANCELLING)
    ]
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset '{dataset}' has {len(active)} active parse task(s). Cancel them first.",
        )

    try:
        shutil.rmtree(path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete dataset: {e}")

    try:
        deleted = delete_dataset_from_s3(dataset)
        if deleted:
            logger.info("Removed %d object(s) from S3 for dataset '%s'", deleted, dataset)
    except Exception:
        logger.warning(
            "Local dataset deleted but S3 cleanup failed for '%s'", dataset,
            exc_info=True,
        )

    logger.info("Deleted raw_files/%s", dataset)
    return ApiResponse(success=True, message=f"Dataset '{dataset}' deleted")


# ---------------------------------------------------------------------------
# Knowledge base endpoints
# ---------------------------------------------------------------------------

@router.get("/knowledge-bases")
async def list_knowledge_bases():
    """List all knowledge bases."""
    base = _kb_dir()
    if not base.exists():
        return {"success": True, "data": []}
    kbs = []
    for kb_path in sorted(p for p in base.iterdir() if p.is_dir()):
        meta = _read_meta(kb_path / "_meta.json")
        kbs.append({"name": kb_path.name, "created_at": meta.get("created_at"), "status": meta.get("status", "ready")})
    return {"success": True, "data": kbs}


@router.post("/knowledge-bases", response_model=ApiResponse)
async def create_knowledge_base(body: KBCreateRequest):
    """Create an empty knowledge base."""
    kb_path = _kb_dir() / body.name
    try:
        kb_path.mkdir(parents=True)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"Knowledge base '{body.name}' already exists")
    _write_meta(kb_path / "_meta.json", {"created_at": datetime.utcnow().isoformat() + "Z"})
    return ApiResponse(success=True, message=f"Knowledge base '{body.name}' created")


@router.get("/knowledge-bases/{kb_name}")
async def get_knowledge_base(kb_name: str):
    """Get knowledge base detail including parsed dataset statuses."""
    kb_path = _kb_dir() / kb_name
    if not kb_path.exists():
        raise HTTPException(status_code=404, detail=f"Knowledge base '{kb_name}' not found")

    kb_meta = _read_meta(kb_path / "_meta.json")
    datasets = []
    parse_modes: set[int] = set()
    for ds_path in sorted(p for p in kb_path.iterdir() if p.is_dir()):
        ds_meta = _read_meta(ds_path / "_meta.json")
        pm = ds_meta.get("process_mode")
        if pm is not None:
            parse_modes.add(pm)
        datasets.append({
            "name": ds_path.name,
            "parsed": bool(ds_meta),
            "process_mode": pm,
            "parsed_at": ds_meta.get("parsed_at"),
        })

    # Derive available query modes from the KB's process_mode.
    # All datasets in a KB must share the same process_mode (enforced at parse
    # time), so we use the single common value; fall back to 0 if none parsed.
    common_parse_mode = parse_modes.pop() if len(parse_modes) == 1 else 0
    available_query_modes = [
        {"id": m.id, "label": m.label, "desc": m.desc}
        for m in get_query_modes(common_parse_mode)
    ]

    return {
        "success": True,
        "name": kb_name,
        "created_at": kb_meta.get("created_at"),
        "status": kb_meta.get("status", "ready"),
        "datasets": datasets,
        "available_query_modes": available_query_modes,
    }


@router.get("/knowledge-bases/{kb_name}/overview")
async def get_kb_overview(
    kb_name: str,
    top_nodes: int = 20,
    sample_relations: int = 15,
):
    """Generate a KB overview for LLM-based tree skeleton generation.

    Returns both structured data (``overview``) and a formatted prompt string
    (``prompt``) ready for injection into HetaGen tree generation context.
    """
    kb_path = _kb_dir() / kb_name
    if not kb_path.exists():
        raise HTTPException(status_code=404, detail=f"Knowledge base '{kb_name}' not found")

    workspace_root = _get_workspace_root()
    try:
        overview = generate_kb_overview(
            kb_name=kb_name,
            workspace_root=workspace_root,
            top_nodes=top_nodes,
            sample_relations=sample_relations,
        )
    except Exception as e:
        logger.error("Failed to generate overview for KB '%s': %s", kb_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate KB overview: {e}")
    return {
        "success": True,
        "overview": overview,
        "prompt": format_for_prompt(overview),
    }


@router.delete("/knowledge-bases/{kb_name}", status_code=202, response_model=ApiResponse)
async def delete_knowledge_base(kb_name: str, purge_db: bool = True):
    """Initiate async deletion of a knowledge base.

    Returns 202 immediately. A daemon thread cancels active tasks, waits for
    them to reach a terminal state, purges DB artifacts, then removes the
    filesystem directory. The KB status field is set to 'deleting' until the
    directory is removed.
    """
    kb_path = _kb_dir() / kb_name
    if not kb_path.exists():
        raise HTTPException(status_code=404, detail=f"Knowledge base '{kb_name}' not found")

    meta = _read_meta(kb_path / "_meta.json")
    if meta.get("status") == "deleting":
        return ApiResponse(success=True, message=f"Knowledge base '{kb_name}' is already being deleted")

    active = [
        t for t in list_tasks(limit=1000)
        if t.metadata.get("kb_name") == kb_name
        and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.CANCELLING)
    ]
    for t in active:
        cancel_task(t.task_id)

    meta["status"] = "deleting"
    _write_meta(kb_path / "_meta.json", meta)

    threading.Thread(
        target=_run_kb_deletion,
        args=(kb_name, kb_path, purge_db, [t.task_id for t in active]),
        daemon=True,
        name=f"hetadb-delete-{kb_name}",
    ).start()

    logger.info("KB '%s' deletion started (active_tasks=%d, purge_db=%s)", kb_name, len(active), purge_db)
    return ApiResponse(success=True, message=f"Knowledge base '{kb_name}' deletion started")


@router.delete("/knowledge-bases/{kb_name}/datasets/{dataset_name}", response_model=ApiResponse)
async def remove_dataset_from_kb(kb_name: str, dataset_name: str):
    """Remove a single dataset from a knowledge base.

    Deletes the processed dataset directory (kb/{kb_name}/{dataset_name}/).
    Raw files under raw_files/{dataset_name}/ are not touched.
    Milvus collections and PG tables for this dataset are also purged.
    """
    kb_path = _kb_dir() / kb_name
    if not kb_path.exists():
        raise HTTPException(status_code=404, detail=f"Knowledge base '{kb_name}' not found")
    dataset_path = kb_path / dataset_name
    if not dataset_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Dataset '{dataset_name}' not found in knowledge base '{kb_name}'",
        )

    active = [
        t for t in list_tasks(limit=1000)
        if t.metadata.get("kb_name") == kb_name
        and t.metadata.get("dataset") == dataset_name
        and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.CANCELLING)
    ]
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset '{dataset_name}' has {len(active)} active parse task(s). Cancel them first.",
        )

    _purge_kb_db(kb_name, [dataset_name], kb_path=kb_path)

    try:
        shutil.rmtree(dataset_path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to remove dataset: {e}")
    logger.info("Removed dataset '%s' from KB '%s'", dataset_name, kb_name)
    return ApiResponse(success=True, message=f"Dataset '{dataset_name}' removed from '{kb_name}'")


@router.post("/knowledge-bases/{kb_name}/parse", response_model=ApiResponse)
async def parse_knowledge_base(kb_name: str, request: ParseRequest):
    """Trigger document processing for one or more datasets into a KB.

    Each dataset is submitted as an independent background task.
    Datasets must exist in raw_files before calling this endpoint.
    """
    kb_path = _kb_dir() / kb_name
    if not kb_path.exists():
        raise HTTPException(status_code=404, detail=f"Knowledge base '{kb_name}' not found")

    if _read_meta(kb_path / "_meta.json").get("status") == "deleting":
        raise HTTPException(status_code=409, detail=f"Knowledge base '{kb_name}' is being deleted")

    raw_base = _raw_files_dir()
    missing = [ds for ds in request.datasets if not (raw_base / ds).exists()]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Datasets not found in raw_files: {missing}",
        )

    # Without force=true, reject if any requested dataset was already parsed.
    if not request.force:
        conflicts = []
        for ds in request.datasets:
            meta = _read_meta(kb_path / ds / "_meta.json")
            if meta:
                conflicts.append({
                    "dataset": ds,
                    "parsed_at": meta.get("parsed_at"),
                    "process_mode": meta.get("process_mode"),
                })
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"{len(conflicts)} dataset(s) already parsed in '{kb_name}'. "
                        "Pass force=true to overwrite."
                    ),
                    "conflicts": conflicts,
                },
            )

    # Reject if any requested dataset already has an active parse task in this KB.
    in_progress = [
        t for t in list_tasks(limit=1000)
        if t.metadata.get("kb_name") == kb_name
        and t.metadata.get("dataset") in request.datasets
        and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.CANCELLING)
    ]
    if in_progress:
        busy = sorted({t.metadata.get("dataset") for t in in_progress})
        raise HTTPException(
            status_code=409,
            detail=f"Dataset(s) {busy} already being parsed in '{kb_name}'. Cancel the running task(s) first.",
        )

    workspace_root = _get_workspace_root()
    executor = _get_parse_executor()
    tasks = []
    for ds in request.datasets:
        task = create_task(
            task_type="file_processing",
            metadata={"kb_name": kb_name, "dataset": ds, "mode": request.mode,
                      "schema_name": request.schema_name},
        )

        # Submit directly to the dedicated pool — non-blocking, all datasets
        # are queued immediately and run concurrently up to parse_max_workers.
        cancel_token = get_cancel_token(task.task_id)
        executor.submit(
            run_file_processing,
            task.task_id, workspace_root, kb_name, ds, request.mode, request.schema_name,
            cancel_token,
        )
        tasks.append({"task_id": task.task_id, "dataset": ds})
        logger.info(
            "Processing task %s created (kb=%s ds=%s mode=%d)",
            task.task_id, kb_name, ds, request.mode,
        )

    return ApiResponse(
        success=True,
        message=f"Processing started for {len(tasks)} dataset(s)",
        data={"tasks": tasks, "mode": request.mode},
    )


# ---------------------------------------------------------------------------
# Task endpoints
# ---------------------------------------------------------------------------

@router.get("/processing/tasks")
async def list_processing_tasks(status: TaskStatus | None = None, limit: int = 50):
    """List processing tasks, optionally filtered by status."""
    return list_tasks(status=status, task_type="file_processing", limit=limit)


@router.get("/processing/tasks/{task_id}")
async def get_processing_task(task_id: str):
    """Get task detail by ID."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/processing/tasks/{task_id}/cancel", response_model=ApiResponse)
async def cancel_processing_task(task_id: str):
    """Cancel a task.

    PENDING tasks are cancelled immediately.
    RUNNING tasks are moved to CANCELLING; the pipeline stops at the next
    stage boundary, rolls back partial data, then transitions to CANCELLED.
    """
    if cancel_task(task_id):
        task = get_task(task_id)
        status = task.status.value if task else "cancelled"
        message = (
            "Task cancellation requested — will stop at next stage boundary"
            if status == TaskStatus.CANCELLING
            else "Task cancelled"
        )
        return ApiResponse(success=True, message=message, data={"task_id": task_id, "status": status})
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    raise HTTPException(
        status_code=400,
        detail=f"Cannot cancel task in '{task.status.value}' state",
    )


@router.get("/processing/config", response_model=ApiResponse)
async def get_processing_config():
    """Get current processing configuration."""
    try:
        mgr = ConfigManager()
        llm = mgr.get_llm_config()
        emb = mgr.get_embedding_config()
        parse_stage = mgr.get_parse_stage_settings()
        chunk_rechunk = mgr.get_chunk_rechunk_settings()
        graph_extraction = mgr.get_graph_config()
        graph_dedup = mgr.get_graph_dedup_config()
        return ApiResponse(
            success=True,
            message="OK",
            data={
                "llm": {
                    "model": llm.model,
                    "max_concurrent_requests": llm.max_concurrent_requests,
                    "timeout": llm.timeout,
                },
                "embedding": {
                    "model": emb.model,
                    "dim": emb.dim,
                    "batch_size": emb.batch_size,
                },
                "parse": {
                    "max_workers": parse_stage.max_workers,
                    "supported_ext": sorted(parse_stage.supported_ext)
                    if isinstance(parse_stage.supported_ext, set)
                    else parse_stage.supported_ext,
                },
                "chunk_rechunk": {
                    "chunk_size": chunk_rechunk.chunk_size,
                    "overlap": chunk_rechunk.overlap,
                    "max_batch_bytes": chunk_rechunk.max_batch_bytes,
                    "max_workers": chunk_rechunk.max_workers,
                    "top_k": chunk_rechunk.top_k,
                    "nprobe": chunk_rechunk.nprobe,
                    "merge_threshold": chunk_rechunk.merge_threshold,
                    "max_rounds": chunk_rechunk.max_rounds,
                    "num_topk_param": chunk_rechunk.num_topk_param,
                },
                "graph_extraction": {
                    "batch_size": graph_extraction.batch_size,
                    "max_workers": graph_extraction.max_workers,
                    "max_file_size_bytes": graph_extraction.max_file_size_bytes,
                    "persist_raw_graph": graph_extraction.persist_raw_graph,
                },
                "graph_dedup": {
                    "parallel_batches": graph_dedup.parallel_batches,
                    "batch_size": graph_dedup.batch_size,
                    "llm_batch_size": graph_dedup.llm_batch_size,
                    "max_rounds": graph_dedup.max_rounds,
                    "milvus_dedup_batch_size": graph_dedup.milvus_dedup_batch_size,
                    "llm_max_retries": graph_dedup.llm_max_retries,
                },
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
