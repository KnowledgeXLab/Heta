"""Optional raw graph persistence for graph_extraction outputs."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hetadb.core.db_build.graph_db.graph_vector import embedding
from hetadb.core.db_build.sql_db.sql_db import (
    create_graph_tables_named,
    get_chunk_source_mapping,
    insert_cluster_chunk_relations_table,
    insert_entities_to_pg_table,
    insert_relations_to_pg_table,
)
from hetadb.core.db_build.vector_db.vector_db import (
    connect_milvus,
    ensure_nodes_collection,
    ensure_rel_collection,
    insert_nodes_records_to_milvus,
    insert_relations_to_milvus,
)

logger = logging.getLogger("hetadb.raw_graph_store")


def _load_jsonl_records(input_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not input_dir.exists():
        return records

    for jsonl_file in sorted(input_dir.glob("*.jsonl")):
        with jsonl_file.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping invalid JSON in %s:%d: %s", jsonl_file, line_no, exc)
    return records


def _normalize_chunk_ids(chunk_ids: Any) -> list[str]:
    if isinstance(chunk_ids, list):
        return [str(cid).strip() for cid in chunk_ids if str(cid).strip()]
    if isinstance(chunk_ids, str):
        try:
            parsed = json.loads(chunk_ids.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(cid).strip() for cid in parsed if str(cid).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
        return [cid.strip().strip("[]'\"") for cid in chunk_ids.split(",") if cid.strip().strip("[]'\"")]
    return []


def _attach_node_embeddings(
    records: list[dict[str, Any]],
    *,
    api_key: str,
    embedding_url: str,
    embedding_model: str,
    embedding_timeout: int,
    max_retries: int,
    retry_delay: int,
    api_batch_size: int,
    embedding_dim: int,
) -> list[dict[str, Any]]:
    if not records:
        return records
    texts = [
        (rec.get("Description") or rec.get("NodeName") or "").strip()
        for rec in records
    ]
    embeddings = embedding(
        texts=texts,
        api_key=api_key,
        embedding_url=embedding_url,
        embedding_model=embedding_model,
        embedding_timeout=embedding_timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        api_batch_size=api_batch_size,
        embedding_dim=embedding_dim,
    )
    for rec, emb in zip(records, embeddings):
        rec["embedding"] = emb
    return records


def _attach_relation_embeddings(
    records: list[dict[str, Any]],
    *,
    api_key: str,
    embedding_url: str,
    embedding_model: str,
    embedding_timeout: int,
    max_retries: int,
    retry_delay: int,
    api_batch_size: int,
    embedding_dim: int,
) -> list[dict[str, Any]]:
    if not records:
        return records
    texts = [
        (
            rec.get("Description")
            or f"{rec.get('Node1', '')}-{rec.get('Relation', '')}-{rec.get('Node2', '')}"
        ).strip()
        for rec in records
    ]
    embeddings = embedding(
        texts=texts,
        api_key=api_key,
        embedding_url=embedding_url,
        embedding_model=embedding_model,
        embedding_timeout=embedding_timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        api_batch_size=api_batch_size,
        embedding_dim=embedding_dim,
    )
    for rec, emb in zip(records, embeddings):
        rec["embedding"] = emb
    return records


def _build_cluster_chunk_relations(
    records: list[dict[str, Any]],
    *,
    chunk_table: str,
    object_type: str,
) -> list[dict[str, Any]]:
    cluster_chunk_mapping: dict[str, list[str]] = {}
    all_chunk_ids: set[str] = set()

    for record in records:
        cluster_id = record.get("Id", "")
        if not cluster_id:
            continue
        chunk_ids = _normalize_chunk_ids(record.get("chunk_id", []))
        if not chunk_ids:
            continue
        cluster_chunk_mapping[cluster_id] = chunk_ids
        all_chunk_ids.update(chunk_ids)

    if not all_chunk_ids:
        return []

    chunk_source_map = get_chunk_source_mapping(list(all_chunk_ids), chunk_table)
    relations: list[dict[str, Any]] = []
    for cluster_id, chunk_ids in cluster_chunk_mapping.items():
        for chunk_id in chunk_ids:
            url = chunk_source_map.get(chunk_id, "")
            if not url:
                continue
            relations.append(
                {
                    "cluster_id": cluster_id,
                    "chunk_id": chunk_id,
                    "url": url,
                    "type": object_type,
                    "meta": {},
                }
            )
    return relations


def persist_raw_graph(
    *,
    graph_dir: Path,
    dataset: str,
    chunk_table: str,
    embedding_api_key: str,
    embedding_url: str,
    embedding_model: str,
    embedding_timeout: int,
    embedding_dim: int,
    embedding_batch_size: int,
    embedding_max_retries: int,
    embedding_retry_delay: int,
) -> None:
    """Persist raw graph_extraction outputs into raw PG tables and Milvus collections."""
    node_records = _load_jsonl_records(graph_dir / "node")
    relation_records = _load_jsonl_records(graph_dir / "relation")

    raw_entities_table = f"{dataset}_raw_entities"
    raw_relations_table = f"{dataset}_raw_relations"
    raw_cluster_chunk_relation_table = f"{dataset}_raw_cluster_chunk_relation"
    raw_entity_collection = f"{dataset}_raw_entity_collection"
    raw_relation_collection = f"{dataset}_raw_relation_collection"

    create_graph_tables_named(
        entities_table=raw_entities_table,
        relations_table=raw_relations_table,
        cluster_chunk_relation_table=raw_cluster_chunk_relation_table,
    )
    if node_records or relation_records:
        connect_milvus()

    if node_records:
        insert_entities_to_pg_table(node_records, raw_entities_table)
        entity_relations = _build_cluster_chunk_relations(
            node_records,
            chunk_table=chunk_table,
            object_type="entity",
        )
        if entity_relations:
            insert_cluster_chunk_relations_table(entity_relations, raw_cluster_chunk_relation_table)
        node_records = _attach_node_embeddings(
            node_records,
            api_key=embedding_api_key,
            embedding_url=embedding_url,
            embedding_model=embedding_model,
            embedding_timeout=embedding_timeout,
            max_retries=embedding_max_retries,
            retry_delay=embedding_retry_delay,
            api_batch_size=embedding_batch_size,
            embedding_dim=embedding_dim,
        )
        insert_nodes_records_to_milvus(
            ensure_nodes_collection(raw_entity_collection, dim=embedding_dim),
            node_records,
        )
        logger.info("Persisted raw entities: %d", len(node_records))

    if relation_records:
        insert_relations_to_pg_table(relation_records, raw_relations_table)
        relation_chunk_relations = _build_cluster_chunk_relations(
            relation_records,
            chunk_table=chunk_table,
            object_type="relation",
        )
        if relation_chunk_relations:
            insert_cluster_chunk_relations_table(relation_chunk_relations, raw_cluster_chunk_relation_table)
        relation_records = _attach_relation_embeddings(
            relation_records,
            api_key=embedding_api_key,
            embedding_url=embedding_url,
            embedding_model=embedding_model,
            embedding_timeout=embedding_timeout,
            max_retries=embedding_max_retries,
            retry_delay=embedding_retry_delay,
            api_batch_size=embedding_batch_size,
            embedding_dim=embedding_dim,
        )
        insert_relations_to_milvus(
            ensure_rel_collection(raw_relation_collection, dim=embedding_dim),
            relation_records,
        )
        logger.info("Persisted raw relations: %d", len(relation_records))
