"""Chunk embedding, database ingestion, and iterative LLM-based merge.

Two main workflows:
1. Ingestion — read chunked JSONL files, generate embeddings via an
   OpenAI-compatible API, then batch-insert into PostgreSQL and Milvus.
2. Merge — iteratively find semantically similar chunks through Milvus
   vector search, ask an LLM to merge/refine them, and update the
   collections in-place.  Round 1 uses a "refine" prompt; subsequent
   rounds use a "merge" prompt.  Iteration stops when the merge ratio
   drops below a configurable threshold.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from pymilvus import Collection

from hetadb.core.db_build.vector_db.vector_db import (
    connect_milvus,
    ensure_chunk_collection,
    get_chunk_text_by_id,
    insert_chunk_batch_milvus,
)
from hetadb.core.db_build.sql_db.sql_db import (
    create_chunk_table,
    batch_insert_chunks_pg,
)
from hetadb.utils.hash_filename import get_sha256_hash

logger = logging.getLogger("hetadb.chunks_merge")


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _embed_chunk(
    chunk: list[str],
    url: str,
    headers: dict,
    embedding_model: str,
    embedding_dim: int,
    max_retries: int,
    retry_delay: int,
) -> list[list[float]]:
    """Call the embedding API for a single chunk with exponential-backoff retry."""
    payload = {"input": chunk, "model": embedding_model}
    current_delay = retry_delay
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            return [item["embedding"] for item in resp.json()["data"]]
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning("Embedding API error: %s. Retrying in %ss...", e, current_delay)
                time.sleep(current_delay)
                current_delay *= 2
            else:
                logger.error("Failed to get embeddings after %s attempts: %s", max_retries, e)
                return [[0.0] * embedding_dim for _ in chunk]


def get_text_embeddings(
    texts: list[str],
    embedding_api_base: str,
    embedding_model: str,
    embedding_api_key: str,
    embedding_dim: int,
    api_batch_size: int = 64,
) -> list[list[float]]:
    """Fetch embeddings via the OpenAI-compatible API.

    Splits *texts* into chunks of at most *api_batch_size* per request to
    stay within the API's per-request limit.  Results are concatenated in
    order so the returned list is always aligned with the input.
    """
    url = f"{embedding_api_base.rstrip('/')}/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {embedding_api_key}",
    }
    results: list[list[float]] = []
    for start in range(0, len(texts), api_batch_size):
        chunk = texts[start : start + api_batch_size]
        results.extend(_embed_chunk(chunk, url, headers, embedding_model, embedding_dim, max_retries=3, retry_delay=2))
    return results


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_jsonl_file(file_path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file and return parsed records."""
    records = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("Parse error at %s:%s: %s", file_path, line_num, e)
    return records


def process_file(file_path: Path) -> list[dict[str, Any]]:
    """Extract chunk records from a single JSONL file."""
    records = read_jsonl_file(file_path)
    chunks = []

    for record in records:
        meta = record.get("meta", {}) or {}
        source = meta.get("source")

        chunk_text = record.get("text", "")
        if not chunk_text or not isinstance(chunk_text, str) or not chunk_text.strip():
            continue

        chunk_text = chunk_text.strip()
        chunk_id = record.get("chunk_id", "")

        chunks.append({
            "chunk_id": chunk_id,
            "text": chunk_text,
            "source": source,
            "source_chunk": json.dumps([chunk_id]),
        })

    return chunks


# ---------------------------------------------------------------------------
# Embedding + DB ingestion
# ---------------------------------------------------------------------------

def process_files_with_embedding(
    file_paths: list[Path],
    collections: list[str],
    write_pg: bool,
    embedding_batch_size: int,
    embedding_num_thread: int,
    embedding_api_base: str,
    embedding_model: str,
    embedding_api_key: str,
    embedding_dim: int,
    postgres_config: dict[str, Any],
    chunk_table: str,
    postgres_batch_size: int,
):
    """Read chunk JSONL files, generate embeddings, and batch-write to databases.

    Files are processed in batches of 10 to control memory usage.  Within each
    file batch, chunks are embedded in sub-batches of ``embedding_batch_size``
    and inserted into all target Milvus collections (and optionally PostgreSQL).
    """
    connect_milvus()

    milvus_collections = {}
    for collection_name in collections:
        milvus_collections[collection_name] = ensure_chunk_collection(collection_name, embedding_dim)

    if write_pg:
        create_chunk_table(chunk_table, postgres_config)

    total_chunks = 0
    total_pg_inserted = 0
    total_milvus_inserted = {name: 0 for name in collections}

    file_batch_size = 10
    for file_batch_idx in range(0, len(file_paths), file_batch_size):
        file_batch = file_paths[file_batch_idx:file_batch_idx + file_batch_size]
        batch_num = file_batch_idx // file_batch_size + 1
        total_batches = (len(file_paths) + file_batch_size - 1) // file_batch_size
        logger.info("Processing file batch %s/%s", batch_num, total_batches)

        all_chunks: list[dict] = []
        with ThreadPoolExecutor(max_workers=embedding_num_thread) as executor:
            futures = {executor.submit(process_file, fp): fp for fp in file_batch}
            for future in as_completed(futures):
                file_path = futures[future]
                try:
                    chunks = future.result()
                    all_chunks.extend(chunks)
                    logger.info("File %s: extracted %s chunks", file_path.name, len(chunks))
                except Exception as e:
                    logger.error("Failed to process file %s: %s", file_path.name, e)

        total_chunks += len(all_chunks)
        logger.info("Batch extracted %s chunks, cumulative %s", len(all_chunks), total_chunks)

        emb_total_batches = (len(all_chunks) + embedding_batch_size - 1) // embedding_batch_size
        for batch_idx in range(0, len(all_chunks), embedding_batch_size):
            batch = all_chunks[batch_idx:batch_idx + embedding_batch_size]
            emb_batch_num = batch_idx // embedding_batch_size + 1

            texts = [chunk["text"] for chunk in batch]
            logger.info("Embedding batch %s/%s: %s texts", emb_batch_num, emb_total_batches, len(texts))
            embeddings = get_text_embeddings(
                texts, embedding_api_base, embedding_model, embedding_api_key, embedding_dim,
            )

            for chunk, embedding in zip(batch, embeddings):
                chunk["embedding"] = embedding

            if write_pg:
                try:
                    batch_insert_chunks_pg(batch, postgres_config, chunk_table, postgres_batch_size)
                    total_pg_inserted += len(batch)
                except Exception as e:
                    logger.error("Batch %s PostgreSQL insert failed: %s", emb_batch_num, e)

            for collection_name, collection in milvus_collections.items():
                try:
                    insert_chunk_batch_milvus(collection, batch)
                    total_milvus_inserted[collection_name] += len(batch)
                except Exception as e:
                    logger.error("Batch %s Milvus insert to %s failed: %s", emb_batch_num, collection_name, e)

    logger.info("Ingestion complete: %s chunks total", total_chunks)
    if write_pg:
        logger.info("PostgreSQL inserted: %s", total_pg_inserted)
    for collection_name, count in total_milvus_inserted.items():
        logger.info("Milvus collection %s inserted: %s", collection_name, count)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ingest_original_chunks(
    data_dir: str,
    chunk_table: str,
    write_pg: bool,
    milvus_collections: list[str],
    embedding_batch_size: int,
    embedding_num_thread: int,
    embedding_api_base: str,
    embedding_model: str,
    embedding_api_key: str,
    embedding_dim: int,
    postgres_config: dict[str, Any],
    postgres_batch_size: int,
) -> None:
    """Ingest chunk JSONL files into PostgreSQL and Milvus collections."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        logger.error("Data directory does not exist: %s", data_dir)
        return

    jsonl_files = list(data_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.warning("No JSONL files found in %s", data_dir)
        return

    logger.info("Found %s JSONL files", len(jsonl_files))
    logger.info("Ingesting to PostgreSQL table=%s, Milvus collections=%s",
                chunk_table, ", ".join(milvus_collections))

    process_files_with_embedding(
        jsonl_files,
        milvus_collections,
        write_pg=write_pg,
        embedding_batch_size=embedding_batch_size,
        embedding_num_thread=embedding_num_thread,
        embedding_api_base=embedding_api_base,
        embedding_model=embedding_model,
        embedding_api_key=embedding_api_key,
        embedding_dim=embedding_dim,
        postgres_config=postgres_config,
        chunk_table=chunk_table,
        postgres_batch_size=postgres_batch_size,
    )

    logger.info("Ingestion complete")

def merge_original_chunks(
    chunks_path: str,
    collection_name: str,
    top_k: int,
    nprobe: int,
    merge_threshold: float,
    max_rounds: int,
    num_topk_param: int,
    num_threads_param: int,
    milvus_host: str,
    milvus_port: int,
    target_merge_collection: str,
    embedding_api_base: str,
    embedding_model: str,
    embedding_api_key: str,
    embedding_dim: int,
    use_llm,
    merge_and_refine_prompt: str,
    merge_prompt: str,
    merged_chunks_file: str | None = None,
) -> None:
    """Run iterative LLM-based merge rounds over previously ingested chunks."""
    embedding_cfg = {
        "embedding_api_base": embedding_api_base,
        "embedding_model": embedding_model,
        "embedding_api_key": embedding_api_key,
        "embedding_dim": embedding_dim,
    }
    run_merge_rounds(
        chunks_path=chunks_path,
        collection_name=collection_name,
        top_k=top_k,
        nprobe=nprobe,
        merge_threshold=merge_threshold,
        max_rounds=max_rounds,
        num_topk_param=num_topk_param,
        num_threads_param=num_threads_param,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        target_merge_collection=target_merge_collection,
        embedding_cfg=embedding_cfg,
        use_llm=use_llm,
        merge_and_refine_prompt=merge_and_refine_prompt,
        merge_prompt=merge_prompt,
        merged_chunks_file=merged_chunks_file,
    )




# ---------------------------------------------------------------------------
# init.json helpers
# ---------------------------------------------------------------------------

def load_init_json(init_path: str) -> dict[str, Any]:
    """Load source-to-chunk_ids mapping from init.json."""
    if not Path(init_path).exists():
        logger.warning("init.json not found: %s, returning empty dict", init_path)
        return {}
    with open(init_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_init_json(init_path: str, data: dict[str, Any]) -> None:
    """Persist the updated init.json mapping."""
    with open(init_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Updated init.json: %s", init_path)


def get_init_mapping(chunks_path: Path) -> dict[str, dict[str, Any]]:
    """Build source -> {chunk_ids, merge_tag, save_ratio} mapping from JSONL files."""
    mapping: dict[str, dict] = {}
    for path in sorted(chunks_path.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception as e:
                    logger.warning("Skip %s:%s parse error: %s", path.name, line_no, e)
                    continue

                meta = record.get("meta", {}) or {}
                source_id = meta.get("source")
                if not source_id:
                    source_id = f"{path.name}-{line_no}"

                cid = record.get("chunk_id")
                if not cid:
                    continue

                if source_id not in mapping:
                    mapping[source_id] = {"chunk_ids": [], "merge_tag": False, "save_ratio": 1}
                mapping[source_id]["chunk_ids"].append(cid)
    return mapping


# ---------------------------------------------------------------------------
# LLM-based merge
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    new_chunk: dict
    removed_ids: list[str]


def llm_merge_once(
    main_text: str,
    candidates: list[tuple[str, str]],
    topk: int,
    prompt: str,
    use_llm,
):
    """Ask the LLM to merge the main chunk with similar candidates."""
    main_obj = {"id": 0, "text": main_text}
    cand_objs = [
        {"id": i + 1, "text": txt}
        for i, (_, txt) in enumerate(candidates[:topk])
    ]

    prompt_text = prompt.format(
        MAIN_CHUNK=json.dumps(main_obj, ensure_ascii=False),
        CANDIDATE_LIST=json.dumps(cand_objs, ensure_ascii=False),
    )

    try:
        resp = use_llm(prompt=prompt_text, response_format={"type": "json_object"})
        out = json.loads(resp)

        merge_id = out.get("merge_id")
        merged_text = out.get("text")
        if merged_text is None:
            return None

        if isinstance(merge_id, int):
            merge_id = [merge_id]
        elif isinstance(merge_id, list):
            merge_id = list(merge_id)

        return {
            "merged_text": merged_text,
            "merge_ids": merge_id,
        }

    except Exception as e:
        logger.debug("LLM merge failed: %s", e)
        return None


def process_single_chunk(
    cid: str,
    source_chunk_ids: list[str],
    collection: Collection,
    prompt: str,
    top_k: int,
    nprobe: int,
    num_topk_param: int,
    embedding_cfg: dict,
    use_llm,
    blocked_ids: set[str],
    lock: threading.Lock,
):
    """Search Milvus for chunks similar to *cid*, ask the LLM to merge them.

    Returns a ``MergeResult`` containing the newly merged chunk (with its
    embedding) and the list of chunk IDs that were absorbed.  The caller's
    ``blocked_ids`` set is updated under *lock* so that concurrent threads
    skip chunks that have already been consumed by another merge.
    """
    with lock:
        if cid in blocked_ids:
            return None

    main_text, main_emb, main_source_chunks, source_id = get_chunk_text_by_id(collection, cid)
    if not main_text:
        return None

    search_params = {"metric_type": "IP", "params": {"nprobe": nprobe}}
    results = collection.search(
        [main_emb],
        "text_embedding",
        param=search_params,
        limit=top_k,
        output_fields=["chunk_id", "text"],
    )

    candidates = []
    for hit in results[0]:
        hid = hit.entity.get("chunk_id")
        if not hid or hid == cid or hid in source_chunk_ids:
            continue
        with lock:
            if hid in blocked_ids:
                continue
        candidates.append((hid, hit.entity.get("text")))

    if not candidates:
        return None

    llm_out = llm_merge_once(main_text, candidates, num_topk_param, prompt, use_llm)
    if not llm_out:
        return None

    id_map = {0: cid}
    for i, (hid, _) in enumerate(candidates[:num_topk_param], start=1):
        id_map[i] = hid
    if llm_out["merge_ids"] is not None:
        removed_ids = [id_map[i] for i in llm_out["merge_ids"] if i in id_map]
    else:
        removed_ids = []

    merged_text = llm_out["merged_text"]
    if not merged_text:
        return None
    new_chunk_id = get_sha256_hash(merged_text)

    new_emb = get_text_embeddings([merged_text], **embedding_cfg)[0]

    source_chunk_new = json.loads(main_source_chunks) + list(dict.fromkeys(removed_ids))

    new_chunk = {
        "chunk_id": new_chunk_id,
        "text": merged_text,
        "embedding": new_emb,
        "source_id": source_id,
        "source_chunk": json.dumps(source_chunk_new),
    }
    removed_ids.append(cid)
    with lock:
        blocked_ids.update(removed_ids)
        blocked_ids.add(new_chunk_id)

    return MergeResult(new_chunk=new_chunk, removed_ids=removed_ids)


# ---------------------------------------------------------------------------
# Multi-round merge orchestration
# ---------------------------------------------------------------------------

def run_merge_rounds(
    chunks_path: str,
    collection_name: str,
    target_merge_collection: str,
    merge_and_refine_prompt: str,
    merge_prompt: str,
    top_k: int,
    nprobe: int,
    merge_threshold: float,
    max_rounds: int,
    num_topk_param: int,
    num_threads_param: int,
    milvus_host: str,
    milvus_port: int,
    embedding_cfg: dict,
    use_llm,
    merged_chunks_file: str = None,
):
    """Run iterative vector-search + LLM merge rounds until convergence.

    Each round processes every source's chunks in parallel: for each chunk,
    Milvus returns the most similar neighbours, and the LLM decides whether
    to merge them.  Merged chunks replace their originals in both Milvus
    and the local init.json mapping.  Round 1 uses ``merge_and_refine_prompt``
    (focused on deduplication); later rounds use ``merge_prompt`` (broader
    consolidation).  The loop stops when the per-round merge ratio falls
    below ``merge_threshold`` or ``max_rounds`` is reached.
    """
    logger.info("Starting merge rounds for collection: %s", collection_name)

    connect_milvus()

    collection = Collection(collection_name)
    collection.load()
    logger.info("Collection %s loaded", collection_name)

    data = get_init_mapping(Path(chunks_path))

    total_chunks = sum(len(v["chunk_ids"]) for v in data.values())
    source_order = sorted(data, key=lambda k: len(data[k]["chunk_ids"]), reverse=True)
    logger.info("Total chunks: %s, sources: %s", total_chunks, len(data))

    if total_chunks == 0:
        logger.warning("No chunks to merge, skipping")
        return

    overall_merged = 0

    if merged_chunks_file:
        merged_chunks_path = Path(merged_chunks_file)
        merged_chunks_path.parent.mkdir(parents=True, exist_ok=True)

    for round_idx in range(1, max_rounds + 1):
        phase = "refine" if round_idx == 1 else "merge"
        prompt = merge_and_refine_prompt if round_idx == 1 else merge_prompt
        logger.info("Round %s/%s (%s phase)", round_idx, max_rounds, phase)

        merged_this_round = 0
        removed_ids_round: set[str] = set()
        merged_chunks_round: list[dict] = []

        for idx, source_id in enumerate(source_order):
            chunk_ids = list(data[source_id]["chunk_ids"])
            if not chunk_ids:
                continue

            logger.info("Source %s (%s chunks) - %s/%s",
                        source_id, len(chunk_ids), idx + 1, len(source_order))

            lock = threading.Lock()
            blocked_ids = removed_ids_round.copy()

            source_removed_ids: set[str] = set()
            source_results: dict[str, MergeResult] = {}

            with ThreadPoolExecutor(max_workers=num_threads_param) as ex:
                future_to_cid = {
                    ex.submit(
                        process_single_chunk,
                        cid, chunk_ids, collection, prompt,
                        top_k, nprobe, num_topk_param,
                        embedding_cfg, use_llm, blocked_ids, lock,
                    ): cid
                    for cid in chunk_ids
                }

                completed_count = 0
                for fu in as_completed(future_to_cid):
                    cid = future_to_cid[fu]
                    res = fu.result()

                    completed_count += 1
                    if completed_count % 10 == 0 or completed_count == len(future_to_cid):
                        logger.info("Completed %s/%s chunks", completed_count, len(future_to_cid))

                    if not res:
                        continue

                    source_results[cid] = res
                    source_removed_ids.update(res.removed_ids)
                    merged_this_round += len(res.removed_ids)

            source_merged_chunks = []
            for cid in chunk_ids:
                if cid in source_results:
                    source_merged_chunks.append(source_results[cid].new_chunk)

            merged_chunks_round.extend(source_merged_chunks)
            removed_ids_round.update(source_removed_ids)

            original_count = len(data[source_id]["chunk_ids"])
            data[source_id]["chunk_ids"] = [
                cid for cid in data[source_id]["chunk_ids"]
                if cid not in source_removed_ids
            ] + [c["chunk_id"] for c in source_merged_chunks]

            logger.info("Source %s: %s -> %s chunks (removed: %s, added: %s)",
                        source_id, original_count, len(data[source_id]["chunk_ids"]),
                        len(source_removed_ids), len(source_merged_chunks))

        # Update Milvus
        if removed_ids_round:
            collection.delete(expr=f'chunk_id in {json.dumps(list(removed_ids_round))}')
            logger.info("Round %s: deleted %s chunks from Milvus", round_idx, len(removed_ids_round))

        if merged_chunks_round:
            target = ensure_chunk_collection(target_merge_collection, embedding_cfg["embedding_dim"])
            insert_chunk_batch_milvus(target, merged_chunks_round)
            logger.info("Round %s: inserted %s merged chunks", round_idx, len(merged_chunks_round))

        # Persist merged chunks to file
        if merged_chunks_file and merged_chunks_round:
            try:
                with open(merged_chunks_path, "a", encoding="utf-8") as f:
                    for chunk in merged_chunks_round:
                        chunk_data = {k: v for k, v in chunk.items() if k != "embedding"}
                        chunk_with_round = {"round": round_idx, "phase": phase, **chunk_data}
                        f.write(json.dumps(chunk_with_round, ensure_ascii=False) + "\n")
                logger.info("Saved %s merged chunks to %s", len(merged_chunks_round), merged_chunks_path)
            except Exception as e:
                logger.warning("Failed to save merged chunks: %s", e)

        # Clean removed IDs from mapping
        for source_id in data:
            data[source_id]["chunk_ids"] = [
                cid for cid in data[source_id]["chunk_ids"]
                if cid not in removed_ids_round
            ]

        save_init_json(str(Path(chunks_path) / "init.json"), data)

        # Convergence check
        round_ratio = merged_this_round / total_chunks if total_chunks else 0
        overall_merged += merged_this_round
        logger.info("Round %s: merged %s chunks, ratio=%.4f", round_idx, merged_this_round, round_ratio)

        if round_ratio < merge_threshold:
            logger.info("Ratio %.4f < threshold %.4f, stopping", round_ratio, merge_threshold)
            break

    logger.info("Merge finished. Total merged: %s", overall_merged)
