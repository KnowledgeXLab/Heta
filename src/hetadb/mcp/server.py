"""HetaDB MCP server.

Exposes HetaDB knowledge-base discovery and query as MCP tools by proxying
requests to the running HetaDB FastAPI service.

Usage:
    python src/hetadb/mcp/server.py
    HETADB_API_BASE=http://host:8001 python src/hetadb/mcp/server.py
"""

import logging
import os
import sys
import types
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
import uvicorn

_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from common.config import setup_logging

_BASE = os.getenv("HETADB_API_BASE", "http://localhost:8000").rstrip("/")

setup_logging("heta", force=True)
logger = logging.getLogger("heta.mcp.hetadb")

# Shared async client — created at module level (httpx connects lazily).
# Disable environment proxy handling to avoid SOCKS proxy validation errors.
_http = httpx.AsyncClient(base_url=_BASE, timeout=60, trust_env=False)

mcp = FastMCP("hetadb", host="0.0.0.0", port=8012)


async def _run_streamable_http_async(self) -> None:
    starlette_app = self.streamable_http_app()
    config = uvicorn.Config(
        starlette_app,
        host=self.settings.host,
        port=self.settings.port,
        log_level=self.settings.log_level.lower(),
        log_config=None,
    )
    server = uvicorn.Server(config)
    await server.serve()


mcp.run_streamable_http_async = types.MethodType(_run_streamable_http_async, mcp)


@mcp.tool()
async def list_knowledge_bases() -> dict:
    """List all available HetaDB knowledge bases.

    Returns the name, creation time, and status of every knowledge base that
    has been created in this HetaDB instance.  Call this first to discover
    which KB to target before calling hetadb_query.

    Each entry in the returned data array has:
        name       (str): The KB identifier — pass this as `kb_id` to hetadb_query.
        created_at (str | None): ISO-8601 timestamp when the KB was created.
        status     (str): Current status, typically "ready" or "processing".

    RETURNS:
        {"success": true, "data": [{"name": "...", "created_at": "...", "status": "ready"}, ...]}

    EXAMPLE — discover available knowledge bases:
        list_knowledge_bases()
        # → {"success": true, "data": [{"name": "my_kb", "created_at": "2026-01-01T00:00:00", "status": "ready"}]}
    """
    r = await _http.get("/api/v1/hetadb/files/knowledge-bases")
    r.raise_for_status()
    return r.json()


_MCP_USER_ID = "mcp-agent"


@mcp.tool()
async def hetadb_query(kb_id: str, query: str, query_mode: str = "naive") -> dict:
    """Query a HetaDB knowledge base with a natural-language question.

    Retrieves relevant content from the specified knowledge base and returns a
    synthesised answer along with source citations. The knowledge base must exist
    and have status "ready" — use list_knowledge_bases to find available KBs.

    Note: querying always searches the full knowledge base. Per-dataset filtering
    is not supported through this tool.

    REQUIRED:
        kb_id  (str): Knowledge base identifier, e.g. "my_kb".
                      Use list_knowledge_bases to discover valid values.
        query  (str): Natural-language question or information need.

    OPTIONAL:
        query_mode (str): Retrieval strategy. Default is "naive" (BM25 + vector +
                          cross-encoder reranking). Currently only "naive" is
                          production-ready; do not pass other values unless you know
                          the KB's registered modes.

    RETURNS:
        {
          "answer": "<synthesised answer text>",
          "citations": [{"index": 1, "source_file": "...", "dataset": "...", "file_url": "..."}] | null
        }
        On backend error: raises RuntimeError with the backend's error message.

    EXAMPLE — ask a question about a knowledge base:
        hetadb_query(kb_id="research_papers", query="What is scaled dot-product attention?")

    EXAMPLE — discover KB first, then query:
        kbs = list_knowledge_bases()
        if any(kb["name"] == "research_papers" for kb in kbs["data"]):
            hetadb_query(kb_id="research_papers", query="Summarise the key findings.")
    """
    r = await _http.post(
        "/api/v1/hetadb/chat",
        json={
            "kb_id": kb_id,
            "query": query,
            "query_mode": query_mode,
            "user_id": _MCP_USER_ID,
        },
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(body.get("message", "hetadb_query failed with unknown error"))
    return {
        "answer": body.get("response"),
        "citations": body.get("citations"),
    }


if __name__ == "__main__":
    logger.info("Starting HetaDB MCP server on 0.0.0.0:8012")
    mcp.run(transport="streamable-http")
