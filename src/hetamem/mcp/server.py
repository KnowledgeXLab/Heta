"""HetaMem MCP server.

Exposes MemoryKB (LightRAG long-term memory) and MemoryVG (personal memory)
as MCP tools by proxying requests to the running HetaMem FastAPI service.

Usage:
    python src/hetamem/mcp/server.py
    HETAMEM_BASE_URL=http://host:8000 python src/hetamem/mcp/server.py
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

_BASE = os.getenv("HETAMEM_BASE_URL", "http://localhost:8000").rstrip("/")

setup_logging("heta", force=True)
logger = logging.getLogger("heta.mcp.hetamem")

# Shared async client — created at module level (httpx connects lazily).
_http = httpx.AsyncClient(base_url=_BASE, timeout=60)

mcp = FastMCP("hetamem", host="0.0.0.0", port=8011)


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


# ── MemoryKB ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def kb_insert(query: str) -> dict:
    """Ingest text (and optional media captions) into the long-term knowledge-base memory.

    The input is processed through a multi-modal pipeline: text is passed directly;
    any attached video, audio, or image is transcribed/captioned first, then merged
    with the text before being fed into LightRAG. LightRAG extracts entities and
    relations, builds a knowledge graph, and stores semantic chunks in a vector index.
    Subsequent queries over the same KB will be able to reason across all inserted content.

    Use this tool to accumulate knowledge that should persist across sessions and be
    available for structured reasoning — documents, reports, domain knowledge, meeting
    notes, etc. For storing conversational facts about a specific user or agent, use
    vg_add instead.

    REQUIRED:
        query (str): The text content to store. Can be a sentence, paragraph, or
                     multi-page document. There is no length limit enforced at this
                     layer, but very large inputs will take longer to process.

    ⚠️  ASYNC — this tool returns immediately with status "accepted".
    The actual insertion (LightRAG entity extraction + graph construction)
    runs in the background and takes 10–60 seconds depending on text length
    and LLM latency.  Do NOT call kb_query right away — wait at least 30
    seconds after kb_insert before querying to ensure the content is indexed.

    RETURNS (202 Accepted):
        {"id": "<uuid>", "query": "<original text>", "status": "accepted", ...}

    EXAMPLE — store a domain knowledge document:
        kb_insert(query="Transformer models use self-attention to capture long-range \
dependencies without recurrence, making them highly parallelisable during training.")
    """
    r = await _http.post("/api/v1/hetamem/kb/insert", data={"query": query})
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_query(query: str, mode: str = "hybrid", use_pm: bool = False) -> dict:
    """Query the long-term knowledge-base memory and return a synthesised answer.

    Retrieves relevant content from the LightRAG knowledge graph and vector index,
    then synthesises a final answer using the configured LLM. The retrieval strategy
    is controlled by the mode parameter. Optionally, a parametric memory model (a
    fine-tuned LLM that has internalised domain knowledge) can be consulted first;
    if its answer is judged relevant it is used directly, bypassing RAG retrieval.

    Use this tool when you need to answer questions that require reasoning over
    structured, persistent knowledge — content previously ingested via kb_insert.

    REQUIRED:
        query (str): The question or information need, expressed in natural language.

    OPTIONAL:
        mode (str): Retrieval strategy. Choose based on the nature of the question:
            - "local"  — retrieves from the immediate neighbourhood of matched entities
                         in the knowledge graph. Best for specific, entity-focused questions.
            - "global" — aggregates high-level community summaries across the full graph.
                         Best for broad thematic or comparative questions.
            - "hybrid" — combines local and global retrieval (default). Best general-purpose
                         choice when the question scope is unknown.
            - "naive"  — plain vector similarity search without graph traversal. Fastest
                         but misses multi-hop relational reasoning.
        use_pm (bool): If True, queries the parametric memory model before attempting RAG.
                       If the model's answer is deemed relevant, it is returned directly
                       without touching the knowledge graph. Default is False.

    RETURNS:
        {"query": "...", "mode": "...", "pm_used": false, "pm_memory": null,
         "pm_relevant": false, "rag_memory": "<retrieved context>", "final_answer": "..."}

    EXAMPLE — answer a specific entity question:
        kb_query(query="What is the role of self-attention in Transformer models?",
                 mode="local")

    EXAMPLE — broad thematic question across the full knowledge base:
        kb_query(query="What are the main architectural trends in modern LLMs?",
                 mode="global")
    """
    r = await _http.post(
        "/api/v1/hetamem/kb/query",
        json={"query": query, "mode": mode, "use_pm": use_pm},
    )
    r.raise_for_status()
    return r.json()


# ── MemoryVG ──────────────────────────────────────────────────────────────────


@mcp.tool()
async def vg_add(
    messages: list[dict],
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Extract key facts from a conversation and persist them as personal memories.

    The LLM automatically decides which information is worth remembering, deduplicates
    against existing memories, and tags each entry with the provided scope identifiers.
    The returned results list contains the memory IDs you will need for later retrieval,
    update, or deletion via other vg_* tools.

    REQUIRED — messages AND at least one scope identifier (user_id / agent_id / run_id):

        messages (list[dict]): Conversation turns to process. Each item must have:
            - "role"    (str): Speaker role, typically "user" or "assistant".
            - "content" (str): The message text.

        user_id   (str): Identifies the human user. Use this to scope memories to a
                         specific person across all their sessions.
        agent_id  (str): Identifies the AI agent. Use this when memories belong to an
                         agent's own persistent knowledge rather than a user.
        run_id    (str): Identifies a single conversation session. Use this to scope
                         memories to one specific interaction only.

        NOTE: scope identifiers can be combined (AND logic). A memory stored with both
        user_id and agent_id will only be retrievable when both are provided together.

    OPTIONAL:
        metadata (dict): Arbitrary key-value pairs attached to every stored memory entry.
                         Useful for tagging source, topic, or priority.

    RETURNS:
        {"results": [{"id": "<uuid>", "memory": "<extracted fact>", "event": "ADD|UPDATE"}]}

    EXAMPLE — store facts from a user conversation:
        messages = [
            {"role": "user",      "content": "I'm allergic to peanuts and I prefer dark mode."},
            {"role": "assistant", "content": "Got it, I'll remember that for you."}
        ]
        vg_add(messages=messages, user_id="alice")
    """
    r = await _http.post(
        "/api/v1/hetamem/vg/add",
        json={
            "messages": messages,
            "user_id": user_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "metadata": metadata,
        },
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_search(
    query: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    limit: int = 10,
    threshold: float | None = None,
) -> dict:
    """Search personal memories by semantic similarity and return the most relevant entries.

    Embeds the query and performs a vector similarity search over stored memories that
    match the given scope. Use this before answering user questions to recall relevant
    context — e.g. preferences, past decisions, or previously stated facts.

    REQUIRED — query AND at least one scope identifier (user_id / agent_id / run_id):

        query     (str): Natural-language question or topic to search for.
                         The search is semantic, so exact wording does not need to match.

        user_id   (str): Scope to memories belonging to a specific user.
        agent_id  (str): Scope to memories belonging to a specific agent.
        run_id    (str): Scope to memories from a specific conversation session.

        NOTE: scope identifiers must exactly match those used when the memories were
        stored with vg_add. Combining multiple IDs narrows results (AND logic).

    OPTIONAL:
        limit     (int):   Maximum number of results to return. Default is 10.
        threshold (float): Minimum similarity score (0–1) to include a result.
                           Higher values return only closely matching memories.
                           Omit to return all results up to limit.

    RETURNS:
        {"results": [{"id": "<uuid>", "memory": "<fact>", "score": 0.91, ...}]}

    EXAMPLE — recall what a user has told you before answering:
        vg_search(query="Does Alice have any dietary restrictions?", user_id="alice")
    """
    r = await _http.post(
        "/api/v1/hetamem/vg/search",
        json={
            "query": query,
            "user_id": user_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "limit": limit,
            "threshold": threshold,
        },
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_list(
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> dict:
    """List all personal memories, optionally filtered by user/agent/run scope."""
    params: dict = {"limit": limit}
    if user_id:
        params["user_id"] = user_id
    if agent_id:
        params["agent_id"] = agent_id
    if run_id:
        params["run_id"] = run_id
    r = await _http.get("/api/v1/hetamem/vg", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_get(memory_id: str) -> dict:
    """Retrieve a single personal memory entry by ID."""
    r = await _http.get(f"/api/v1/hetamem/vg/{memory_id}")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_history(memory_id: str) -> list:
    """Return the full modification history of a personal memory entry."""
    r = await _http.get(f"/api/v1/hetamem/vg/{memory_id}/history")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_update(memory_id: str, data: str) -> dict:
    """Update the text content of a personal memory entry."""
    r = await _http.put(
        f"/api/v1/hetamem/vg/{memory_id}",
        json={"data": data},
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_delete(memory_id: str) -> dict:
    """Delete a single personal memory entry by ID."""
    r = await _http.delete(f"/api/v1/hetamem/vg/{memory_id}")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def vg_delete_all(
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Delete all personal memories matching the given scope filters."""
    params: dict = {}
    if user_id:
        params["user_id"] = user_id
    if agent_id:
        params["agent_id"] = agent_id
    if run_id:
        params["run_id"] = run_id
    r = await _http.delete("/api/v1/hetamem/vg", params=params)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    logger.info("Starting HetaMem MCP server on 0.0.0.0:8011")
    mcp.run(transport="streamable-http")
