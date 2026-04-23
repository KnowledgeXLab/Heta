"""
Heta unified API entry point.

Aggregates HetaDB, HetaGen, and HetaMem routers under a single FastAPI application.

HetaDB endpoints   : /chat, /files, /config
HetaGen endpoints  : /hetagen/gentable, /hetagen/tag-tree
HetaMem endpoints  : /api/v1/memorykb/*, /api/v1/memoryvg/*
"""

import logging
import subprocess
import sys
from contextlib import asynccontextmanager

from common.config import setup_logging, load_config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

setup_logging("heta", force=True)
logger = logging.getLogger("heta")

from hetadb.core.db_build.vector_db.vector_db import ensure_milvus_databases
from hetadb.api.routers import chat_router, files_router, config_router, schema_router
from hetagen.api.routers import pipeline, tag_tree
from hetamem.api.routers import kb_router, vg_router
from hetawiki.core.api.routers import ingest as hetawiki_ingest
from hetawiki.core.api.routers import lint as hetawiki_lint
from hetawiki.core.api.routers import query as hetawiki_query
from hetawiki.core.api.routers import tasks as hetawiki_tasks
from hetawiki.core.api.routers import wiki as hetawiki_wiki
from hetawiki.core.wiki import scheduler as hetawiki_scheduler
from hetamem.memkb_manager import manager as kb_manager
from hetamem.memvg_manager import manager as vg_manager
from hetamem.utils.path import PACKAGE_ROOT as HETAMEM_ROOT
from hetadb.utils.path import PACKAGE_ROOT as HETADB_ROOT

cfg = load_config("heta")

_HETAMEM_MCP_SCRIPT = HETAMEM_ROOT / "mcp" / "server.py"
_HETADB_MCP_SCRIPT = HETADB_ROOT / "mcp" / "server.py"
_hetamem_mcp_proc: subprocess.Popen | None = None
_hetadb_mcp_proc: subprocess.Popen | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _hetamem_mcp_proc, _hetadb_mcp_proc

    logger.info("Ensuring Milvus databases...")
    ensure_milvus_databases()
    logger.info("Initializing MemoryKB...")
    await kb_manager.initialize()
    logger.info("Initializing MemoryVG...")
    await vg_manager.initialize()

    logger.info("Starting HetaMem MCP server (%s)...", _HETAMEM_MCP_SCRIPT)
    _hetamem_mcp_proc = subprocess.Popen([sys.executable, str(_HETAMEM_MCP_SCRIPT)])
    logger.info("HetaMem MCP server started (pid=%s, port=8011)", _hetamem_mcp_proc.pid)

    logger.info("Starting HetaDB MCP server (%s)...", _HETADB_MCP_SCRIPT)
    _hetadb_mcp_proc = subprocess.Popen([sys.executable, str(_HETADB_MCP_SCRIPT)])
    logger.info("HetaDB MCP server started (pid=%s, port=8012)", _hetadb_mcp_proc.pid)

    logger.info("Starting HetaWiki lint scheduler...")
    hetawiki_scheduler.start()

    yield

    hetawiki_scheduler.stop()

    if _hetamem_mcp_proc and _hetamem_mcp_proc.poll() is None:
        _hetamem_mcp_proc.terminate()
        _hetamem_mcp_proc.wait()
        logger.info("HetaMem MCP server stopped")

    if _hetadb_mcp_proc and _hetadb_mcp_proc.poll() is None:
        _hetadb_mcp_proc.terminate()
        _hetadb_mcp_proc.wait()
        logger.info("HetaDB MCP server stopped")


app = FastAPI(
    title=cfg.get("title", "Heta API"),
    version=cfg.get("version", "0.1.0"),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HetaDB routers
app.include_router(chat_router)
app.include_router(files_router)
app.include_router(config_router)
app.include_router(schema_router)

# HetaGen routers
app.include_router(pipeline.router)
app.include_router(tag_tree.router)

# HetaMem routers
app.include_router(kb_router)
app.include_router(vg_router)

# HetaWiki routers
app.include_router(hetawiki_ingest.router)
app.include_router(hetawiki_lint.router)
app.include_router(hetawiki_query.router)
app.include_router(hetawiki_tasks.router)
app.include_router(hetawiki_wiki.router)


@app.get("/")
async def root():
    return {"service": cfg.get("title"), "version": cfg.get("version")}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    fastapi_cfg = cfg.get("fastapi", {})
    host = fastapi_cfg.get("host", "0.0.0.0")
    port = fastapi_cfg.get("port", 8000)

    logger.info("Starting Heta API on %s:%s", host, port)

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=fastapi_cfg.get("reload", False),
        log_level=fastapi_cfg.get("log_level", "info"),
        log_config=None,
    )
