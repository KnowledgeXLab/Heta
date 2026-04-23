"""Graph dedup stage for HetaDB pipelines."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hetadb.core.db_build.graph_db.merge_mappings import merge_mappings_adaptive
from hetadb.core.db_build.graph_db.node_dedup_merge import (
    dedup_nodes,
    embed_nodes,
    run_merge_pipeline,
    run_milvus_dedup,
)
from hetadb.core.db_build.graph_db.rel_dedup_merge import (
    dedup_relations,
    embed_rels,
    run_rel_merge_pipeline,
    run_rel_milvus_dedup,
)
from hetadb.core.db_build.sql_db.sql_db import query_cluster_chunk_relations_by_urls


logger = logging.getLogger("hetadb.graph_dedup")


@dataclass
class GraphDedupPaths:
    """Path and identifier model required by the graph_dedup stage only."""

    raw_files_dir: Path
    graph_dir: Path
    original_kg_node_input_path: Path
    dedup_kg_node_output_path: Path
    dedup_kg_node_embedding_output_path: Path
    batch_kg_node_merge_output_path: Path
    final_kg_node_merge_output_path: Path
    mapping_path: Path
    original_kg_rel_input_path: Path
    dedup_kg_rel_output_path: Path
    dedup_kg_rel_embedding_output_path: Path
    batch_kg_rel_merge_output_path: Path
    final_kg_rel_merge_output_path: Path
    kb_name: str
    dataset: str


@dataclass
class GraphDedupConfig:
    """Minimal config required by the graph_dedup stage only."""

    use_llm: Any
    embedding_cfg: dict[str, Any]
    llm_max_concurrent_requests: int
    merge_max_rounds: int
    merge_llm_batch_size: int
    merge_batch_size: int
    merge_parallel_batches: int
    merge_llm_max_retries: int
    merge_milvus_dedup_batch_size: int
    merge_node_top_k: int
    merge_node_sim_threshold: float
    merge_node_temperature: float
    merge_rel_top_k: int
    merge_rel_sim_threshold: float
    merge_rel_temperature: float
    embedding_api_key: str
    embedding_url: str
    embedding_model: str
    embedding_timeout: int
    embedding_batch_size: int
    embedding_max_file_size_bytes: int
    embedding_num_threads: int
    embedding_max_retries: int
    embedding_retry_delay: int
    embedding_dim: int
    dedup_template: str
    merge_cluster_prompt: str
    dedup_rel_template: str
    merge_rel_prompt: str


@dataclass
class GraphDedupStageResult:
    """Structured outputs produced by the graph_dedup stage."""

    has_mapping: bool
    final_node_files: int
    final_relation_files: int
    exported_cluster_relation_records: int

    def has_any_output(self) -> bool:
        return (
            self.has_mapping
            or self.final_node_files > 0
            or self.final_relation_files > 0
            or self.exported_cluster_relation_records > 0
        )


def build_graph_dedup_paths(
    workspace_root: Path,
    kb_name: str,
    dataset: str,
) -> GraphDedupPaths:
    """Build graph_dedup-stage paths without depending on DatasetPaths."""
    base = workspace_root / "kb" / kb_name / dataset
    return GraphDedupPaths(
        raw_files_dir=workspace_root / "raw_files" / dataset,
        graph_dir=base / "kg_file",
        original_kg_node_input_path=base / "kg_file" / "node" / "nodes_0000.jsonl",
        dedup_kg_node_output_path=base / "kg_file" / "dedup" / "dedup_node.jsonl",
        dedup_kg_node_embedding_output_path=base / "kg_file" / "dedup_node_emb",
        batch_kg_node_merge_output_path=base / "kg_file" / "batch_merge_nodes",
        final_kg_node_merge_output_path=base / "kg_file" / "final_nodes",
        mapping_path=base / "kg_file" / "final_nodes" / "final_mapping.json",
        original_kg_rel_input_path=base / "kg_file" / "relation" / "relations_0000.jsonl",
        dedup_kg_rel_output_path=base / "kg_file" / "dedup_rel" / "dedup_rel.jsonl",
        dedup_kg_rel_embedding_output_path=base / "kg_file" / "dedup_rel_emb",
        batch_kg_rel_merge_output_path=base / "kg_file" / "batch_merge_rels",
        final_kg_rel_merge_output_path=base / "kg_file" / "final_res",
        kb_name=kb_name,
        dataset=dataset,
    )


def _count_jsonl_dir(input_dir: Path) -> int:
    if not input_dir.exists():
        return 0
    return len(list(input_dir.glob("*.jsonl")))


def _count_jsonl_records(input_file: Path) -> int:
    if not input_file.exists():
        return 0
    count = 0
    with input_file.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _run_node_processing(paths: GraphDedupPaths, config: GraphDedupConfig) -> None:
    paths.dedup_kg_node_output_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    dedup_nodes(
        use_llm=config.use_llm,
        dedup_template=config.dedup_template,
        input_path=paths.original_kg_node_input_path,
        output_path=paths.dedup_kg_node_output_path,
        workers=config.llm_max_concurrent_requests,
        max_rounds=config.merge_max_rounds,
        llm_batch_size=config.merge_llm_batch_size,
    )
    logger.info("Node dedup done in %.1fs", time.time() - start)

    start = time.time()
    embed_nodes(
        api_key=config.embedding_api_key,
        embedding_url=config.embedding_url,
        embedding_model=config.embedding_model,
        embedding_timeout=config.embedding_timeout,
        nodes_input_path=paths.dedup_kg_node_output_path,
        output_dir=paths.dedup_kg_node_embedding_output_path,
        batch_size=config.embedding_batch_size,
        max_file_size_bytes=config.embedding_max_file_size_bytes,
        num_threads=config.embedding_num_threads,
        max_retries=config.embedding_max_retries,
        retry_delay=config.embedding_retry_delay,
        embedding_dim=config.embedding_dim,
    )
    logger.info("Node embedding done in %.1fs", time.time() - start)

    start = time.time()
    run_merge_pipeline(
        embedding_dir=paths.dedup_kg_node_embedding_output_path,
        output_dir=paths.batch_kg_node_merge_output_path,
        use_llm=config.use_llm,
        emb_cfg=config.embedding_cfg,
        merge_cluster_prompt=config.merge_cluster_prompt,
        batch_size=config.merge_batch_size,
        n=config.merge_parallel_batches,
        sim_threshold=config.merge_node_sim_threshold,
        temperature=config.merge_node_temperature,
        max_workers=config.llm_max_concurrent_requests,
        llm_max_retries=config.merge_llm_max_retries,
    )
    run_milvus_dedup(
        input_data_dir=str(paths.batch_kg_node_merge_output_path),
        output_data_dir=str(paths.final_kg_node_merge_output_path),
        use_llm=config.use_llm,
        merge_cluster_prompt=config.merge_cluster_prompt,
        dataset=f"{paths.kb_name}__{paths.dataset}",
        emb_cfg=config.embedding_cfg,
        top_k=config.merge_node_top_k,
        batch_size=config.merge_milvus_dedup_batch_size,
        max_workers=config.llm_max_concurrent_requests,
        temperature=config.merge_node_temperature,
        llm_max_retries=config.merge_llm_max_retries,
    )
    merge_mappings_adaptive(
        batch_merge_dir=str(paths.batch_kg_node_merge_output_path),
        final_nodes_dir=str(paths.final_kg_node_merge_output_path),
        output_dir=str(paths.final_kg_node_merge_output_path),
    )
    logger.info("Node merge done in %.1fs", time.time() - start)


def _run_relation_processing(paths: GraphDedupPaths, config: GraphDedupConfig) -> None:
    paths.dedup_kg_rel_output_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    dedup_relations(
        use_llm=config.use_llm,
        rel_dedup_prompt=config.dedup_rel_template,
        input_path=paths.original_kg_rel_input_path,
        mapping_path=paths.mapping_path,
        output_path=paths.dedup_kg_rel_output_path,
        workers=config.llm_max_concurrent_requests,
        max_rounds=config.merge_max_rounds,
        llm_batch_size=config.merge_llm_batch_size,
    )
    logger.info("Relation dedup done in %.1fs", time.time() - start)

    start = time.time()
    embed_rels(
        api_key=config.embedding_api_key,
        embedding_url=config.embedding_url,
        embedding_model=config.embedding_model,
        embedding_timeout=config.embedding_timeout,
        rels_input_path=paths.dedup_kg_rel_output_path,
        output_dir=paths.dedup_kg_rel_embedding_output_path,
        batch_size=config.embedding_batch_size,
        max_file_size_bytes=config.embedding_max_file_size_bytes,
        num_threads=config.embedding_num_threads,
        max_retries=config.embedding_max_retries,
        retry_delay=config.embedding_retry_delay,
        embedding_dim=config.embedding_dim,
    )
    logger.info("Relation embedding done in %.1fs", time.time() - start)

    start = time.time()
    run_rel_merge_pipeline(
        embedding_dir=paths.dedup_kg_rel_embedding_output_path,
        output_dir=paths.batch_kg_rel_merge_output_path,
        use_llm=config.use_llm,
        emb_cfg=config.embedding_cfg,
        merge_rel_prompt=config.merge_rel_prompt,
        batch_size=config.merge_batch_size,
        n=config.merge_parallel_batches,
        sim_threshold=config.merge_rel_sim_threshold,
        temperature=config.merge_rel_temperature,
        max_workers=config.llm_max_concurrent_requests,
        llm_max_retries=config.merge_llm_max_retries,
    )
    run_rel_milvus_dedup(
        input_data_dir=str(paths.batch_kg_rel_merge_output_path),
        output_data_dir=str(paths.final_kg_rel_merge_output_path),
        use_llm=config.use_llm,
        merge_rel_prompt=config.merge_rel_prompt,
        dataset=f"{paths.kb_name}__{paths.dataset}",
        emb_cfg=config.embedding_cfg,
        top_k=config.merge_rel_top_k,
        batch_size=config.merge_milvus_dedup_batch_size,
        max_workers=config.llm_max_concurrent_requests,
        temperature=config.merge_rel_temperature,
        llm_max_retries=config.merge_llm_max_retries,
    )
    logger.info("Relation merge done in %.1fs", time.time() - start)


def _export_cluster_chunk_relations(paths: GraphDedupPaths) -> int:
    start = time.time()
    source_ids = (
        [f.name for f in paths.raw_files_dir.iterdir() if f.is_file()]
        if paths.raw_files_dir.exists()
        else []
    )
    if not source_ids:
        logger.warning("No source files found in %s, skipping export", paths.raw_files_dir)
        return 0

    logger.info("Querying cluster-chunk relations for %d source files", len(source_ids))
    relations = query_cluster_chunk_relations_by_urls(
        source_ids,
        f"{paths.kb_name}__{paths.dataset}",
    )
    if not relations:
        logger.warning("No cluster-chunk relations found")
        return 0

    output_dir = paths.graph_dir / "cluster"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "cluster_chunk_relations.jsonl"
    with output_file.open("w", encoding="utf-8") as f:
        for relation in relations:
            f.write(json.dumps(relation, ensure_ascii=False) + "\n")
    logger.info(
        "Exported %d cluster-chunk relations to %s in %.1fs",
        len(relations),
        output_file,
        time.time() - start,
    )
    return len(relations)


def run_graph_dedup_stage(
    paths: GraphDedupPaths,
    config: GraphDedupConfig,
) -> GraphDedupStageResult:
    """Deduplicate and merge nodes/relations, then export cluster-chunk relations."""
    _run_node_processing(paths, config)
    _run_relation_processing(paths, config)
    exported_cluster_relation_records = _export_cluster_chunk_relations(paths)
    return GraphDedupStageResult(
        has_mapping=paths.mapping_path.exists(),
        final_node_files=_count_jsonl_dir(paths.final_kg_node_merge_output_path),
        final_relation_files=_count_jsonl_dir(paths.final_kg_rel_merge_output_path),
        exported_cluster_relation_records=exported_cluster_relation_records,
    )
