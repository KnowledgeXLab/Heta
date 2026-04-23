"""Chat query service layer.

Implements multi-dataset retrieval, scoring, and answer generation
for the knowledge-base chat API.  All functions here are framework-agnostic;
the FastAPI router in ``hetadb.api.routers.chat`` is the sole consumer.

Query dispatch is keyed by ``(process_mode, query_mode_id)`` — see
:mod:`hetadb.core.mode_registry` for the full registry.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from hetadb.utils.load_config import get_query_defaults, get_chat_cfg
from hetadb.core.organize.multi_hop_qa import MultiHopAgent
from hetadb.core.organize.query_rewriter import QueryRewriter
from hetadb.core.retrieval.ans_by_sql import DescriptiveText2SQLEngine
from hetadb.core.retrieval.kb_querier import (
    generate_answer_from_content,
    get_top_k_items,
    optimized_kb_query,
    execute_sql_query,
    generate_answer,
    bm25_search_chunks,
    get_reranker,
)
from common.llm_client import create_use_llm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_response(request_id: str, code: int, message: str) -> dict:
    """Build a standardised error payload."""
    return {
        "success": False,
        "message": message,
        "data": [],
        "total_count": 0,
        "request_id": request_id,
        "code": code,
        "query_info": {},
        "response": None,
    }


_workspace_root: Path | None = None


def _get_workspace_root() -> Path:
    """Return the resolved workspace root (cached after first call)."""
    global _workspace_root
    if _workspace_root is None:
        from hetadb.core.file_processor import ConfigManager
        _workspace_root = ConfigManager().get_workspace_root()
    return _workspace_root


def load_kb_datasets(kb_name: str) -> list[str]:
    """Return the list of parsed dataset names under *kb_name*.

    Scans ``workspace/kb/{kb_name}/`` for subdirectories that contain a
    ``_meta.json`` (i.e. datasets that completed processing).
    """
    kb_path = _get_workspace_root() / "kb" / kb_name
    if not kb_path.exists():
        return []
    return sorted(
        p.name for p in kb_path.iterdir()
        if p.is_dir() and (p / "_meta.json").exists()
    )


def _extract_final_answer(answer_trace: list | None) -> str | None:
    """Extract the final answer from a multi-hop agent trace.

    Walks the trace in reverse, looking for an explicit answer payload.
    Deliberately ignores intermediate thoughts / actions so they do not leak
    into the public response.
    """
    if not answer_trace:
        return None

    for block in reversed(answer_trace):
        if not isinstance(block, dict):
            continue
        raw = block.get("answer")
        if not raw:
            continue
        cleaned = str(raw).strip()
        if not cleaned:
            continue
        if "Final Answer:" in cleaned:
            cleaned = cleaned.split("Final Answer:", 1)[1].strip()
        lowered = cleaned.lower()
        if lowered.startswith(("thought:", "question:", "action:", "observation:", "memory:")):
            continue
        return cleaned
    return None


# Injected as the sole context item when KB retrieval returns no results.
# The LLM sees this and responds on behalf of Heta rather than hallucinating.
_NO_RESULTS_CONTEXT = (
    "[System notice: The knowledge base returned no relevant content for this query. "
    "You are Heta, an AI assistant. Respond politely to the user, explain that no "
    "relevant information was found in the current knowledge base, and suggest they "
    "try rephrasing their question or verify that the knowledge base contains the "
    "relevant content.]"
)


# ---------------------------------------------------------------------------
# Citation builder
# ---------------------------------------------------------------------------

def _build_citations(
    topk_chunk: list[str],
    chunk_data_map: dict[str, dict],
    chunk_to_db_prefix: dict[str, str],
    kb_id: str,
) -> list[dict]:
    """Build a deduplicated list of file-level citations from retrieved chunks.

    Each unique (dataset, source_file) pair produces one citation entry with
    an optional S3 presigned URL.  Chunks whose source_id is empty are skipped.
    """
    from hetadb.utils.file_url import get_file_url

    citations: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for chunk_id in topk_chunk:
        cr = chunk_data_map.get(chunk_id, {})
        if cr.get("status") != "success":
            continue
        source_file = cr["data"].get("source_id", "")
        if not source_file:
            continue
        dataset = chunk_to_db_prefix.get(chunk_id, f"{kb_id}__unknown").split("__", 1)[-1]
        key = (dataset, source_file)
        if key in seen:
            continue
        seen.add(key)
        citations.append({
            "index": len(citations) + 1,
            "source_file": source_file,
            "dataset": dataset,
            "file_url": get_file_url(dataset, source_file),
        })

    return citations


def _materialize_retrieval_provenance(
    data_items: list,
    topk_chunk: list[str],
    chunk_data_map: dict[str, dict],
    chunk_to_db_prefix: dict[str, str],
    kb_id: str,
) -> tuple[list[dict], list[str], list[int | None]]:
    """Build citations plus chunk-aligned content/source labels for answering."""
    citations = _build_citations(topk_chunk, chunk_data_map, chunk_to_db_prefix, kb_id)
    cite_key_to_idx = {(c["dataset"], c["source_file"]): c["index"] for c in citations}

    content_texts: list[str] = []
    source_labels: list[int | None] = []
    for item in data_items:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
        if not content:
            continue
        chunk_id = ((item.get("source_id") or [None])[0]) if isinstance(item, dict) else None
        label: int | None = None
        if chunk_id and chunk_id in chunk_data_map:
            cr = chunk_data_map[chunk_id]
            if cr.get("status") == "success":
                src_file = cr["data"].get("source_id", "")
                dataset = chunk_to_db_prefix.get(chunk_id, f"{kb_id}__unknown").split("__", 1)[-1]
                label = cite_key_to_idx.get((dataset, src_file))
        content_texts.append(content)
        source_labels.append(label)

    return citations, content_texts, source_labels


# ---------------------------------------------------------------------------
# Single-dataset query (sync, runs inside a thread)
# ---------------------------------------------------------------------------

def _run_sql_queries(
    query: str,
    res: list,
    dataset: str,
    kb_name: str,
    max_workers: int = 4,
) -> list[dict]:
    """Execute SQL queries against table nodes in parallel."""
    table_info_dir = str(
        _get_workspace_root() / "kb" / kb_name / dataset / "parsed_file" / "table_info"
    )
    table_names = [
        item["nodename"] for item in res if item.get("type") == "table"
    ]
    if not table_names:
        return []

    sql_results: list[dict] = []

    def _query_single_table(table_name: str) -> dict:
        result = execute_sql_query(
            query=query, table_info_dir=table_info_dir, table_name=table_name,
        )
        result["dataset"] = dataset
        result["table_name"] = table_name
        return result

    with ThreadPoolExecutor(max_workers=min(max_workers, len(table_names))) as pool:
        futures = {pool.submit(_query_single_table, t): t for t in table_names}
        for future in as_completed(futures):
            table = futures[future]
            try:
                sql_results.append(future.result())
            except Exception:
                logger.warning(
                    "SQL query failed for dataset=%s table=%s", dataset, table,
                    exc_info=True,
                )
    return sql_results


def perform_single_dataset_query(
    query: str,
    top_k: int,
    threshold: float,
    dataset: str,
    embedding: list[float],
    request_id: str,
    kb_name: str,
) -> dict:
    """Run a full retrieval pipeline against a single dataset.

    Stages:
        1. (parallel) chunk vector pre-filter + KG retrieval
        2. (parallel) DB batch lookup + table-level SQL queries
        3. composite chunk scoring → top-K selection
    """
    # All DB artifacts are prefixed with {kb_name}__{dataset}
    db_prefix = f"{kb_name}__{dataset}"

    empty_result = {
        "chunks": ([], {}),
        "kg": ([], False),
        "entities": [],
        "relations": [],
        "sql_result": None,
    }

    try:
        # Stage 1: chunk pre-filter + KG retrieval (parallel)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_chunk = pool.submit(
                optimized_kb_query.get_top_similar_chunks,
                query=query, top_k=1000, dataset=db_prefix, embedding=embedding,
            )
            future_kg = pool.submit(
                optimized_kb_query.query_kg_source,
                query=query, top_k=top_k, threshold=threshold,
                dataset=db_prefix, embedding=embedding,
            )
            allowed_chunk_ids, precomputed_scores = future_chunk.result()
            kg_res, use_db = future_kg.result()

        table_nodes = [item for item in kg_res if item.get("type") == "table"]
        logger.info(
            "[%s] use_db=%s, table nodes in kg_res=%d", request_id, use_db, len(table_nodes),
        )

        # Stage 2: DB batch lookup + SQL queries (parallel)
        entity_res, relation_res, chunks_res = [], [], []
        sql_results = None
        futures: dict[str, any] = {}

        with ThreadPoolExecutor(max_workers=2) as pool:
            if kg_res:
                futures["db"] = pool.submit(
                    optimized_kb_query.query_by_res_batch_optimized,
                    kg_res,
                    f"{db_prefix}_entities",
                    f"{db_prefix}_relations",
                    allowed_chunk_ids=allowed_chunk_ids,
                    dataset=db_prefix,
                )
            if use_db:
                futures["sql"] = pool.submit(
                    _run_sql_queries, query=query, res=kg_res,
                    dataset=dataset, kb_name=kb_name,
                )
            for name, fut in futures.items():
                try:
                    if name == "db":
                        entity_res, relation_res, chunks_res = fut.result()
                    elif name == "sql":
                        sql_results = fut.result()
                except Exception:
                    logger.warning(
                        "[%s] dataset '%s' %s query failed",
                        request_id, dataset, name, exc_info=True,
                    )

        # Stage 3: composite chunk scoring
        if chunks_res:
            defaults = get_query_defaults()
            similarity_weight = defaults["similarity_weight"]
            occur_weight = defaults["occur_weight"]
            counter = Counter(chunks_res)
            chunk_scores = {
                cid: precomputed_scores.get(cid, 0.0) * similarity_weight
                + occur * occur_weight
                for cid, occur in counter.items()
            }
            top_chunk_ids = get_top_k_items(chunk_scores, top_k)
            return {
                "chunks": (list(top_chunk_ids), {c: chunk_scores[c] for c in top_chunk_ids}),
                "kg": (kg_res, use_db),
                "entities": entity_res,
                "relations": relation_res,
                "sql_result": sql_results,
            }

        # Fallback: no KG-boosted chunks — use pure vector ranking
        top_ids = allowed_chunk_ids[:top_k]
        return {
            "chunks": (top_ids, {c: precomputed_scores.get(c, 0.0) for c in top_ids}),
            "kg": (kg_res, use_db),
            "entities": [],
            "relations": [],
            "sql_result": sql_results,
        }

    except Exception:
        logger.error(
            "[%s] dataset '%s' query failed (fatal)", request_id, dataset, exc_info=True,
        )
        return empty_result


# ---------------------------------------------------------------------------
# Multi-dataset orchestrator
# ---------------------------------------------------------------------------

async def perform_knowledge_query(  # noqa: PLR0912, PLR0915
    query: str,
    top_k: int | None = None,
    kb_id: str | None = None,
    kb_name: str | None = None,
    user_id: str | None = None,
    max_results: int = 20,
    request_id: str | None = None,
) -> dict:
    """Concurrently query multiple datasets and aggregate results."""
    try:
        datasets = load_kb_datasets(kb_id)
        if not datasets:
            logger.warning("[%s] kb_id '%s' has no parsed datasets", request_id, kb_id)
            return _error_response(request_id, 400, f"kb_id '{kb_id}' has no parsed datasets")

        defaults = get_query_defaults()
        threshold = defaults["threshold"]
        similarity_weight = defaults["similarity_weight"]
        occur_weight = defaults["occur_weight"]
        top_k = top_k or defaults["top_k"]

        # Compute embedding once
        t0 = time.time()
        embedding = await asyncio.to_thread(
            optimized_kb_query.connection_manager.get_embedding, prompt=query,
        )
        embedding_time = time.time() - t0
        logger.info("[%s] embedding computed in %.3fs", request_id, embedding_time)

        # Dispatch per-dataset queries concurrently
        t_query = time.time()
        tasks = [
            asyncio.create_task(asyncio.to_thread(
                perform_single_dataset_query,
                query=query, top_k=top_k, threshold=threshold,
                dataset=ds, embedding=embedding,
                request_id=f"{request_id}-{ds}", kb_name=kb_id,
            ))
            for ds in datasets
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge results across datasets.
        # chunk_to_db_prefix maps chunk_id → "{kb_id}__{dataset}" for PG lookups.
        all_chunk_ids: set[str] = set()
        all_chunk_scores: dict[str, float] = {}
        chunk_to_db_prefix: dict[str, str] = {}
        all_kg: list[dict] = []
        all_sql: list[dict] = []
        use_db = False
        ok_count = 0

        for ds, result in zip(datasets, raw_results):
            if isinstance(result, Exception):
                logger.error("[%s] dataset '%s' query failed: %s", request_id, ds, result)
                continue
            ok_count += 1
            chunk_ids, scores = result["chunks"]
            kg_items, ds_use_db = result["kg"]
            db_prefix = f"{kb_id}__{ds}"
            for cid in chunk_ids:
                chunk_to_db_prefix[cid] = db_prefix
            all_chunk_ids.update(chunk_ids)
            all_chunk_scores.update(scores)
            all_kg.extend(kg_items)
            if ds_use_db:
                use_db = True
            if result.get("sql_result"):
                all_sql.append({"dataset": ds, "result": result["sql_result"]})

        dataset_query_time = time.time() - t_query
        logger.info(
            "[%s] queried %d/%d datasets in %.3fs, %d chunks, %d KG items",
            request_id, ok_count, len(datasets), dataset_query_time,
            len(all_chunk_ids), len(all_kg),
        )

        # Global chunk re-scoring
        t_score = time.time()
        occurrences = Counter(all_chunk_ids)
        score_dict = {
            cid: all_chunk_scores.get(cid, 0.0) * similarity_weight
            + occurrences[cid] * occur_weight
            for cid in all_chunk_ids
            if cid in all_chunk_scores
        }
        topk_chunk = get_top_k_items(score_dict, max_results)
        chunk_time = time.time() - t_score

        # Format results
        t_fmt = time.time()
        format_res = []

        # SQL table results
        if use_db and all_sql:
            for sql_item in all_sql:
                try:
                    for row in sql_item["result"]:
                        if row["answer"] != "None":
                            format_res.append({
                                "kb_id": kb_id or "",
                                "kb_name": row["dataset"],
                                "score": 1,
                                "content": row["answer"],
                                "text": "",
                                "source_id": [f"sql_table_{row['table_name']}"],
                            })
                except Exception:
                    logger.error(
                        "[%s] error processing SQL result", request_id, exc_info=True,
                    )

        # Chunk results — group by DB prefix so each batch queries the right PG table
        chunk_data_map: dict[str, dict] = {}
        if topk_chunk:
            default_prefix = f"{kb_id}__{datasets[0]}"
            prefix_to_chunks: dict[str, list[str]] = {}
            for cid in topk_chunk:
                prefix = chunk_to_db_prefix.get(cid, default_prefix)
                prefix_to_chunks.setdefault(prefix, []).append(cid)

            for prefix, cids in prefix_to_chunks.items():
                partial = await asyncio.to_thread(
                    optimized_kb_query.query_chunks_by_ids_batch, cids, prefix,
                )
                chunk_data_map.update(partial)
            for chunk_id in topk_chunk:
                chunk_result = chunk_data_map.get(chunk_id, {})
                if chunk_result.get("status") == "success":
                    format_res.append({
                        "kb_id": kb_id or "",
                        "kb_name": chunk_result["data"].get("source_id", ""),
                        "score": score_dict[chunk_id],
                        "content": chunk_result["data"]["content_text"],
                        "text": "",
                        "source_id": [chunk_id],
                    })
                else:
                    logger.warning(
                        "[%s] chunk %s lookup returned status=%s",
                        request_id, chunk_id, chunk_result.get("status", "unknown"),
                    )

        format_time = time.time() - t_fmt
        total_time = time.time() - t0
        logger.info(
            "[%s] total=%.3fs (embed=%.3fs query=%.3fs score=%.3fs fmt=%.3fs) → %d results",
            request_id, total_time, embedding_time, dataset_query_time,
            chunk_time, format_time, len(format_res),
        )

        return {
            "success": True,
            "message": "Query completed",
            "data": format_res,
            "total_count": len(format_res),
            "request_id": request_id,
            "code": 200,
            "query_info": {
                "query": query,
                "kb_id": kb_id,
                "datasets": datasets,
                "kb_name": kb_name,
                "user_id": user_id,
                "top_k": top_k,
                "threshold": threshold,
                "similarity_weight": similarity_weight,
                "occur_weight": occur_weight,
                "use_db": use_db,
                "allowed_chunks_count": len(all_chunk_ids),
                "timing": {
                    "embedding": round(embedding_time, 3),
                    "dataset_query": round(dataset_query_time, 3),
                    "chunk_scoring": round(chunk_time, 3),
                    "format_results": round(format_time, 3),
                    "total_time": round(total_time, 3),
                },
            },
            # Internal fields consumed by _naive_kb_response for citation building.
            # Prefixed with "_" to signal they are not part of the public API schema.
            "_topk_chunk": list(topk_chunk),
            "_chunk_data_map": chunk_data_map,
            "_chunk_to_db_prefix": chunk_to_db_prefix,
        }

    except Exception:
        logger.error("[%s] knowledge query failed", request_id, exc_info=True)
        return _error_response(request_id, 500, "Internal query error")


# ---------------------------------------------------------------------------
# Mode 0 — naive KB retrieval + answer generation
# ---------------------------------------------------------------------------

async def _naive_kb_response(params: dict) -> dict:
    """Retrieve from KB then generate an answer (mode 0)."""
    defaults = get_query_defaults()
    top_k = params.get("top_k") or defaults["top_k"]
    max_results = params.get("max_results") or 20
    request_id = str(uuid.uuid4())

    result = await perform_knowledge_query(
        query=params["query"],
        top_k=top_k,
        kb_id=params.get("kb_id"),
        kb_name="",
        user_id=params.get("user_id"),
        max_results=max_results,
        request_id=request_id,
    )
    if not result.get("success"):
        return result

    topk_chunk = result.pop("_topk_chunk", [])
    chunk_data_map = result.pop("_chunk_data_map", {})
    chunk_to_db_prefix = result.pop("_chunk_to_db_prefix", {})

    kb_id = params.get("kb_id", "")
    citations, content_texts, source_labels = _materialize_retrieval_provenance(
        result.get("data", []),
        topk_chunk,
        chunk_data_map,
        chunk_to_db_prefix,
        kb_id,
    )

    if not content_texts:
        logger.info("[%s] retrieval returned no content, using no-results prompt", request_id)
        content_texts = [_NO_RESULTS_CONTEXT]
        source_labels = [None]

    t0 = time.time()
    response_text = await generate_answer_from_content(
        query=params["query"], content_texts=content_texts, request_id=request_id,
        source_labels=source_labels,
    )
    logger.info("[%s] answer generated in %.3fs", request_id, time.time() - t0)

    result["response"] = response_text
    result["citations"] = citations
    return result


# ---------------------------------------------------------------------------
# Mode 1 — query-rewriter
# ---------------------------------------------------------------------------

def _build_rewriter() -> QueryRewriter:
    """Construct a QueryRewriter backed by the unified sync LLM client."""
    cfg = get_chat_cfg()
    sync_llm = create_use_llm(
        url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        timeout=cfg.get("timeout", 120),
        max_retries=cfg.get("max_retries", 3),
    )
    return QueryRewriter(use_llm=sync_llm)


async def _query_rewriter_response(params: dict) -> dict:
    """Generate query variations, retrieve for each, then synthesise (mode 1)."""
    defaults = get_query_defaults()
    top_k = params.get("top_k") or defaults["top_k"]
    max_results = params.get("max_results") or 20
    request_id = str(uuid.uuid4())

    rewriter = _build_rewriter()
    # rewrite() is synchronous (blocking LLM call) — run in a thread pool so
    # the event loop stays free for other requests while we wait for the LLM.
    variations = await asyncio.to_thread(rewriter.rewrite, params["query"], 3)
    logger.info("[%s] rewriter produced %d variations", request_id, len(variations))

    # If rewrite failed entirely, fall back to a single naive retrieval on the
    # original query rather than returning a 500.
    if not variations:
        logger.warning("[%s] query rewrite returned no variations, falling back to naive", request_id)
        return await _naive_kb_response(params)

    tasks = [
        asyncio.create_task(perform_knowledge_query(
            query=v, top_k=top_k, kb_id=params.get("kb_id"),
            kb_name="", user_id=params.get("user_id"),
            max_results=max_results, request_id=f"{request_id}-{i}",
        ))
        for i, v in enumerate(variations, 1)
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    aggregated: list[dict] = []
    successful: list[dict] = []
    errors: list[str] = []
    aggregated_topk_chunk: list[str] = []
    aggregated_chunk_data_map: dict[str, dict] = {}
    aggregated_chunk_to_db_prefix: dict[str, str] = {}
    seen_chunk_ids: set[str] = set()

    for variation, res in zip(variations, raw_results):
        if isinstance(res, Exception):
            errors.append(f"variation '{variation}' raised {res}")
            logger.error("[%s] variation '%s' failed: %s", request_id, variation, res)
            continue
        if not isinstance(res, dict) or not res.get("success"):
            msg = res.get("message", "unknown") if isinstance(res, dict) else str(type(res))
            errors.append(f"variation '{variation}' failed: {msg}")
            continue

        successful.append(res)
        topk_chunk = res.pop("_topk_chunk", [])
        chunk_data_map = res.pop("_chunk_data_map", {})
        chunk_to_db_prefix = res.pop("_chunk_to_db_prefix", {})
        aggregated_chunk_data_map.update(chunk_data_map)
        aggregated_chunk_to_db_prefix.update(chunk_to_db_prefix)
        for cid in topk_chunk:
            if cid in seen_chunk_ids:
                continue
            seen_chunk_ids.add(cid)
            aggregated_topk_chunk.append(cid)
        for item in res.get("data", []):
            aggregated.append(item)

    if not successful:
        logger.error("[%s] all %d variations failed", request_id, len(variations))
        return _error_response(request_id, 500, "All query variations failed")

    kb_id = params.get("kb_id", "")
    citations, content_texts, source_labels = _materialize_retrieval_provenance(
        aggregated,
        aggregated_topk_chunk,
        aggregated_chunk_data_map,
        aggregated_chunk_to_db_prefix,
        kb_id,
    )

    if not content_texts:
        logger.info("[%s] all variations returned no content, using no-results prompt", request_id)
        content_texts = [_NO_RESULTS_CONTEXT]
        source_labels = [None]

    t0 = time.time()
    response_text = await generate_answer_from_content(
        query=params["query"], content_texts=content_texts, request_id=request_id,
        source_labels=source_labels,
    )
    logger.info("[%s] answer generated in %.3fs", request_id, time.time() - t0)

    base = successful[0]
    base["data"] = aggregated
    base["total_count"] = len(aggregated)
    base["response"] = response_text
    base["citations"] = citations
    qi = base.get("query_info", {})
    qi["variations"] = variations
    if errors:
        qi["errors"] = errors
    base["query_info"] = qi
    return base


# ---------------------------------------------------------------------------
# Mode 2 — multi-hop reasoning
# ---------------------------------------------------------------------------

async def _multi_hop_response(params: dict) -> dict:
    """Run multi-hop agent and wrap output (mode 2)."""
    defaults = get_query_defaults()
    top_k = params.get("top_k") or defaults["top_k"]
    max_results = params.get("max_results") or 20
    request_id = f"multi-hop-{uuid.uuid4()}"
    agent = MultiHopAgent()

    try:
        # agent.answer() runs a synchronous ReAct loop with multiple blocking LLM
        # calls — offload to a thread so the event loop stays responsive.
        raw_answer = await asyncio.to_thread(
            agent.answer,
            query=params["query"],
            top_n=top_k,
            score_threshold=defaults["threshold"],
            max_rounds=3,
            collection_name="Multi_hop",
            kb_id=params.get("kb_id"),
            kb_name="",
            user_id=params.get("user_id"),
            max_results=max_results,
            request_id=request_id,
        )
    except Exception:
        logger.error("[%s] multi-hop agent failed", request_id, exc_info=True)
        return _error_response(request_id, 500, "Multi-hop reasoning failed")

    answer_trace = raw_answer.get("trace", []) if isinstance(raw_answer, dict) else raw_answer
    retrieval_trace = raw_answer.get("retrieval_trace", []) if isinstance(raw_answer, dict) else []

    final_answer = _extract_final_answer(answer_trace)
    aggregated_topk_chunk: list[str] = []
    aggregated_chunk_data_map: dict[str, dict] = {}
    aggregated_chunk_to_db_prefix: dict[str, str] = {}
    seen_chunk_ids: set[str] = set()
    data_entries: list[dict] = []
    seen_data_keys: set[tuple[str, str]] = set()

    for step in retrieval_trace:
        for cid in step.get("_topk_chunk", []):
            if cid in seen_chunk_ids:
                continue
            seen_chunk_ids.add(cid)
            aggregated_topk_chunk.append(cid)
        aggregated_chunk_data_map.update(step.get("_chunk_data_map", {}))
        aggregated_chunk_to_db_prefix.update(step.get("_chunk_to_db_prefix", {}))
        for item in step.get("data", []):
            if not isinstance(item, dict):
                continue
            source_ids = item.get("source_id") or []
            source_key = source_ids[0] if source_ids else ""
            content_key = item.get("content", "")
            dedupe_key = (source_key, content_key)
            if dedupe_key in seen_data_keys:
                continue
            seen_data_keys.add(dedupe_key)
            data_entries.append(item)

    kb_id = params.get("kb_id", "")
    citations, _, _ = _materialize_retrieval_provenance(
        data_entries,
        aggregated_topk_chunk,
        aggregated_chunk_data_map,
        aggregated_chunk_to_db_prefix,
        kb_id,
    )

    _, content_texts, source_labels = _materialize_retrieval_provenance(
        data_entries,
        aggregated_topk_chunk,
        aggregated_chunk_data_map,
        aggregated_chunk_to_db_prefix,
        kb_id,
    )

    # If the agent explored but failed to emit an explicit final answer,
    # synthesise one from the retrieved evidence instead of returning a hard
    # failure or leaking internal thoughts.
    if final_answer is None and content_texts:
        t0 = time.time()
        final_answer = await generate_answer_from_content(
            query=params["query"],
            content_texts=content_texts,
            request_id=request_id,
            source_labels=source_labels,
        )
        logger.info("[%s] multi-hop fallback answer generated in %.3fs", request_id, time.time() - t0)

    success = bool(final_answer)

    if success and final_answer is not None and not data_entries:
        data_entries.append({
            "kb_id": kb_id or "",
            "kb_name": "",
            "score": 1.0,
            "content": final_answer,
            "text": final_answer,
            "source_id": [],
        })

    return {
        "success": success,
        "message": "Multi-hop reasoning completed" if success else "Multi-hop produced no answer",
        "data": data_entries,
        "total_count": len(data_entries),
        "request_id": request_id,
        "code": 200 if success else 500,
        "response": final_answer,
        "citations": citations,
        "query_info": {
            "query": params["query"],
            "kb_id": kb_id,
            "kb_name": "",
            "user_id": params.get("user_id"),
            "top_k": top_k,
            "max_results": max_results,
            "multi_hop_trace": answer_trace,
            "multi_hop_retrieval_trace": retrieval_trace,
        },
    }


# ---------------------------------------------------------------------------
# Mode 3 — direct LLM answer (no retrieval)
# ---------------------------------------------------------------------------

async def _naive_response(params: dict) -> dict:
    """Generate an answer without any KB retrieval (mode 3)."""
    request_id = f"naive_{int(time.time())}"
    t0 = time.time()

    try:
        logger.info("[%s] generating direct LLM answer for '%s'", request_id, params["query"])
        response_text = await generate_answer(query=params["query"])
        elapsed = time.time() - t0
        logger.info("[%s] answer generated in %.3fs", request_id, elapsed)

        return {
            "success": True,
            "message": "Direct answer generated",
            "data": [],
            "total_count": 0,
            "request_id": request_id,
            "code": 200,
            "response": response_text,
            "query_info": {
                "query": params["query"],
                "strategy": "naive_response",
                "kb_id": params.get("kb_id"),
                "user_id": params.get("user_id"),
                "timing": {
                    "total_time": round(elapsed, 3),
                    "generate_response": round(elapsed, 3),
                },
            },
        }
    except Exception:
        logger.error("[%s] direct answer generation failed", request_id, exc_info=True)
        return _error_response(request_id, 500, "Answer generation failed")


# ---------------------------------------------------------------------------
# Rerank mode — BM25 + vector → RRF fusion → cross-encoder reranking
# ---------------------------------------------------------------------------

def _rrf_merge(
    ranked_lists: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion over multiple ranked ID lists.

    ``rrf_score(doc) = Σ 1 / (k + rank_in_list_i)`` where rank is 1-based.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def _perform_single_dataset_query_rerank(
    query: str,
    top_k: int,
    threshold: float,
    dataset: str,
    embedding: list[float],
    request_id: str,
    kb_name: str,
    bm25_top_k: int = 200,
) -> dict:
    """BM25 + vector retrieval with RRF fusion — no SQL."""
    db_prefix = f"{kb_name}__{dataset}"
    empty_result = {"chunks": ([], {}), "kg": ([], False), "entities": [], "relations": [], "sql_result": None}

    try:
        # Stage 1: parallel vector search + BM25 search
        # Both lists use the same top_k so neither signal dominates RRF.
        with ThreadPoolExecutor(max_workers=3) as pool:
            future_vec = pool.submit(
                optimized_kb_query.get_top_similar_chunks,
                query=query, top_k=bm25_top_k, dataset=db_prefix, embedding=embedding,
            )
            future_bm25 = pool.submit(bm25_search_chunks, query, db_prefix, bm25_top_k)
            future_kg = pool.submit(
                optimized_kb_query.query_kg_source,
                query=query, top_k=top_k, threshold=threshold,
                dataset=db_prefix, embedding=embedding,
            )
            vector_ids, vector_scores = future_vec.result()
            bm25_ids = future_bm25.result()
            kg_res, use_db = future_kg.result()

        # Stage 2: RRF fusion of vector + BM25 ranked lists
        rrf_scores = _rrf_merge([vector_ids, bm25_ids])
        candidate_ids = list(rrf_scores.keys())

        # Stage 3: KG DB lookup + SQL queries (parallel)
        entity_res, relation_res, chunks_res = [], [], []
        sql_results = None
        futures: dict[str, any] = {}

        with ThreadPoolExecutor(max_workers=2) as pool:
            if kg_res:
                futures["db"] = pool.submit(
                    optimized_kb_query.query_by_res_batch_optimized,
                    kg_res,
                    f"{db_prefix}_entities",
                    f"{db_prefix}_relations",
                    allowed_chunk_ids=candidate_ids,
                    dataset=db_prefix,
                )
            if use_db:
                futures["sql"] = pool.submit(
                    _run_sql_queries, query=query, res=kg_res,
                    dataset=dataset, kb_name=kb_name,
                )
            for name, fut in futures.items():
                try:
                    if name == "db":
                        entity_res, relation_res, chunks_res = fut.result()
                    elif name == "sql":
                        sql_results = fut.result()
                except Exception:
                    logger.warning(
                        "[%s] dataset '%s' %s query failed",
                        request_id, dataset, name, exc_info=True,
                    )

        # Blend KG occurrence boost into RRF scores.
        # RRF scores are O(1/k) ≈ 0.003–0.016 for k=60; scale occur_weight down
        # by k so one occurrence ≈ a rank-1 RRF contribution rather than ~60×.
        if chunks_res:
            defaults = get_query_defaults()
            occur_weight = defaults["occur_weight"] / 60.0
            counter = Counter(chunks_res)
            for cid, cnt in counter.items():
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + cnt * occur_weight

        # Select top-K candidates for reranking
        top_candidates = list(get_top_k_items(rrf_scores, min(top_k * 3, 50)))

        return {
            "chunks": (top_candidates, {c: rrf_scores.get(c, 0.0) for c in top_candidates}),
            "kg": (kg_res, False),
            "entities": entity_res,
            "relations": relation_res,
            "sql_result": sql_results,
        }

    except Exception:
        logger.error(
            "[%s] dataset '%s' rerank query failed", request_id, dataset, exc_info=True,
        )
        return empty_result


async def _perform_knowledge_query_rerank(
    query: str,
    top_k: int | None = None,
    kb_id: str | None = None,
    kb_name: str | None = None,
    user_id: str | None = None,
    max_results: int = 20,
    request_id: str | None = None,
) -> dict:
    """Multi-dataset BM25+vector+RRF retrieval with optional cross-encoder reranking."""
    try:
        datasets = load_kb_datasets(kb_id)
        if not datasets:
            logger.warning("[%s] kb_id '%s' has no parsed datasets", request_id, kb_id)
            return _error_response(request_id, 400, f"kb_id '{kb_id}' has no parsed datasets")

        defaults = get_query_defaults()
        threshold = defaults["threshold"]
        top_k = top_k or defaults["top_k"]

        t0 = time.time()
        embedding = await asyncio.to_thread(
            optimized_kb_query.connection_manager.get_embedding, prompt=query,
        )
        embedding_time = time.time() - t0
        logger.info("[%s] embedding computed in %.3fs", request_id, embedding_time)

        t_query = time.time()
        tasks = [
            asyncio.create_task(asyncio.to_thread(
                _perform_single_dataset_query_rerank,
                query=query, top_k=top_k, threshold=threshold,
                dataset=ds, embedding=embedding,
                request_id=f"{request_id}-{ds}", kb_name=kb_id,
            ))
            for ds in datasets
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        all_rrf_scores: dict[str, float] = {}
        chunk_to_db_prefix: dict[str, str] = {}
        all_sql: list[dict] = []
        ok_count = 0

        for ds, result in zip(datasets, raw_results):
            if isinstance(result, Exception):
                logger.error("[%s] dataset '%s' rerank query failed: %s", request_id, ds, result)
                continue
            ok_count += 1
            chunk_ids, scores = result["chunks"]
            db_prefix = f"{kb_id}__{ds}"
            for cid in chunk_ids:
                chunk_to_db_prefix[cid] = db_prefix
                all_rrf_scores[cid] = all_rrf_scores.get(cid, 0.0) + scores.get(cid, 0.0)
            if result.get("sql_result"):
                all_sql.append({"dataset": ds, "result": result["sql_result"]})

        dataset_query_time = time.time() - t_query
        logger.info(
            "[%s] rerank queried %d/%d datasets in %.3fs, %d candidates",
            request_id, ok_count, len(datasets), dataset_query_time, len(all_rrf_scores),
        )

        # Select candidates, fetch content, then cross-encode rerank
        candidates = list(get_top_k_items(all_rrf_scores, min(max_results * 3, 50)))

        t_fmt = time.time()
        format_res = []
        chunk_data_map: dict[str, dict] = {}
        valid_with_score: list[tuple[str, str, float]] = []

        # SQL table results (prepended so they appear before chunk results)
        for sql_item in all_sql:
            try:
                for row in sql_item["result"]:
                    if row["answer"] != "None":
                        format_res.append({
                            "kb_id": kb_id or "",
                            "kb_name": row["dataset"],
                            "score": 1,
                            "content": row["answer"],
                            "text": "",
                            "source_id": [f"sql_table_{row['table_name']}"],
                        })
            except Exception:
                logger.error("[%s] error processing SQL result", request_id, exc_info=True)

        if candidates:
            default_prefix = f"{kb_id}__{datasets[0]}"
            prefix_to_chunks: dict[str, list[str]] = {}
            for cid in candidates:
                prefix = chunk_to_db_prefix.get(cid, default_prefix)
                prefix_to_chunks.setdefault(prefix, []).append(cid)

            for prefix, cids in prefix_to_chunks.items():
                partial = await asyncio.to_thread(
                    optimized_kb_query.query_chunks_by_ids_batch, cids, prefix,
                )
                chunk_data_map.update(partial)

            # Build (chunk_id, text) pairs for reranking
            valid: list[tuple[str, str]] = []
            for cid in candidates:
                cr = chunk_data_map.get(cid, {})
                if cr.get("status") == "success":
                    valid.append((cid, cr["data"]["content_text"]))

            # Cross-encoder reranking (optional — skipped if no reranker configured)
            t_rerank = time.time()
            if valid:
                try:
                    reranker = get_reranker()
                    if reranker is not None:
                        passages = [text for _, text in valid]
                        rerank_scores = await asyncio.to_thread(
                            reranker.score, query, passages,
                        )
                        paired = sorted(
                            zip(valid, rerank_scores),
                            key=lambda x: x[1],
                            reverse=True,
                        )
                        valid_with_score = [(cid, text, score) for (cid, text), score in paired]
                        logger.info(
                            "[%s] cross-encoder reranked %d passages in %.3fs",
                            request_id, len(valid), time.time() - t_rerank,
                        )
                    else:
                        logger.debug("[%s] no reranker configured, using RRF order", request_id)
                        valid_with_score = [(cid, text, all_rrf_scores.get(cid, 0.0)) for cid, text in valid]
                except Exception:
                    logger.warning(
                        "[%s] reranker failed, falling back to RRF order", request_id, exc_info=True,
                    )
                    valid_with_score = [(cid, text, all_rrf_scores.get(cid, 0.0)) for cid, text in valid]
            else:
                valid_with_score = []

            for cid, text, score in valid_with_score[:max_results]:
                src_file = chunk_data_map.get(cid, {}).get("data", {}).get("source_id", "")
                format_res.append({
                    "kb_id": kb_id or "",
                    "kb_name": src_file,
                    "score": score,
                    "content": text,
                    "text": "",
                    "source_id": [cid],
                })

        format_time = time.time() - t_fmt
        total_time = time.time() - t0
        logger.info(
            "[%s] rerank total=%.3fs (embed=%.3fs query=%.3fs fmt=%.3fs) → %d results",
            request_id, total_time, embedding_time, dataset_query_time, format_time, len(format_res),
        )

        # candidates is the ordered list used for reranking; chunk_data_map was
        # built above.  Expose them for citation building in _rerank_kb_response.
        topk_for_citations = [cid for cid, _, _ in valid_with_score[:max_results]]
        return {
            "success": True,
            "message": "Query completed",
            "data": format_res,
            "total_count": len(format_res),
            "request_id": request_id,
            "code": 200,
            "query_info": {
                "query": query,
                "kb_id": kb_id,
                "datasets": datasets,
                "kb_name": kb_name,
                "user_id": user_id,
                "top_k": top_k,
                "threshold": threshold,
                "allowed_chunks_count": len(all_rrf_scores),
                "timing": {
                    "embedding": round(embedding_time, 3),
                    "dataset_query": round(dataset_query_time, 3),
                    "format_results": round(format_time, 3),
                    "total_time": round(total_time, 3),
                },
            },
            "_topk_chunk": topk_for_citations,
            "_chunk_data_map": chunk_data_map,
            "_chunk_to_db_prefix": chunk_to_db_prefix,
        }

    except Exception:
        logger.error("[%s] rerank knowledge query failed", request_id, exc_info=True)
        return _error_response(request_id, 500, "Internal query error")


async def _rerank_kb_response(params: dict) -> dict:
    """BM25 + vector RRF retrieval with cross-encoder reranking (mode 0/rerank)."""
    defaults = get_query_defaults()
    top_k = params.get("top_k") or defaults["top_k"]
    max_results = params.get("max_results") or 20
    request_id = str(uuid.uuid4())

    result = await _perform_knowledge_query_rerank(
        query=params["query"],
        top_k=top_k,
        kb_id=params.get("kb_id"),
        kb_name="",
        user_id=params.get("user_id"),
        max_results=max_results,
        request_id=request_id,
    )
    if not result.get("success"):
        return result

    topk_chunk = result.pop("_topk_chunk", [])
    chunk_data_map = result.pop("_chunk_data_map", {})
    chunk_to_db_prefix = result.pop("_chunk_to_db_prefix", {})

    kb_id = params.get("kb_id", "")
    citations, content_texts, source_labels = _materialize_retrieval_provenance(
        result.get("data", []),
        topk_chunk,
        chunk_data_map,
        chunk_to_db_prefix,
        kb_id,
    )

    if not content_texts:
        logger.info("[%s] rerank retrieval returned no content, using no-results prompt", request_id)
        content_texts = [_NO_RESULTS_CONTEXT]
        source_labels = [None]

    t0 = time.time()
    response_text = await generate_answer_from_content(
        query=params["query"], content_texts=content_texts, request_id=request_id,
        source_labels=source_labels,
    )
    logger.info("[%s] answer generated in %.3fs", request_id, time.time() - t0)

    result["response"] = response_text
    result["citations"] = citations
    return result


# ---------------------------------------------------------------------------
# Public entry point — mode dispatcher
# ---------------------------------------------------------------------------

# Keyed by (process_mode, query_mode_id).
# To add a new strategy: register it in mode_registry.REGISTRY and add the
# corresponding handler entry here.
_HANDLERS: dict[tuple[int, str], any] = {
    (0, "naive"):    _naive_kb_response,
    (0, "rerank"):   _rerank_kb_response,
    (0, "rewriter"): _query_rewriter_response,
    (0, "multihop"): _multi_hop_response,
    (0, "direct"):   _naive_response,
}


async def query_chat(params: dict) -> dict:
    """Dispatch a chat query to the appropriate mode handler.

    Args:
        params: dict with keys ``query``, ``top_k``, ``kb_id``, ``user_id``,
                ``max_results``, ``process_mode``, ``query_mode``.

    Returns:
        Standardised response dict consumed by the API layer.
    """
    process_mode = params.get("process_mode", 0)
    query_mode = params.get("query_mode", "naive")
    key = (process_mode, query_mode)
    handler = _HANDLERS.get(key)
    if handler is None:
        logger.warning(
            "Unknown (process_mode=%d, query_mode=%s), falling back to naive",
            process_mode, query_mode,
        )
        handler = _naive_kb_response

    result = await handler(params)

    # Final guard: ensure response is never None or blank on a successful reply.
    if result.get("success") and not (result.get("response") or "").strip():
        logger.warning(
            "Handler for (%d, %s) returned empty response, applying fallback",
            process_mode, query_mode,
        )
        result["response"] = (
            "Heta was unable to generate a response for your query. "
            "Please try rephrasing or check if the knowledge base contains relevant content."
        )

    return result
