"""Graph extraction stage for HetaDB pipelines."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hetadb.core.db_build.graph_db.graph_extraction import (
    batch_extract_kg_from_chunks,
    load_chunks_from_jsonl,
)
from hetadb.core.db_build.graph_db.raw_graph_store import persist_raw_graph


logger = logging.getLogger("hetadb.graph_extraction")


@dataclass
class GraphExtractionPaths:
    """Path and identifier model required by the graph_extraction stage only."""

    rechunk_output_dir: Path
    graph_dir: Path
    kb_name: str
    dataset: str


@dataclass
class GraphExtractionConfig:
    """Minimal config required by the graph_extraction stage only."""

    entity_schema_str: str
    relation_schema_str: str
    use_llm: Any
    prompts: dict[str, str]
    batch_size: int
    max_workers: int
    max_file_size_bytes: int
    show_progress: bool = True
    persist_raw_graph: bool = False
    embedding_api_key: str = ""
    embedding_url: str = ""
    embedding_model: str = ""
    embedding_timeout: int = 30
    embedding_dim: int = 1024
    embedding_batch_size: int = 2000
    embedding_max_retries: int = 5
    embedding_retry_delay: int = 2


@dataclass
class GraphExtractionStageResult:
    """Structured outputs produced by the graph_extraction stage."""

    chunk_count: int
    node_count: int
    relation_count: int
    node_files: int
    relation_files: int
    persisted_raw_graph: bool

    def has_any_output(self) -> bool:
        return self.node_count > 0 or self.relation_count > 0


def build_graph_extraction_paths(
    workspace_root: Path,
    kb_name: str,
    dataset: str,
) -> GraphExtractionPaths:
    """Build graph_extraction-stage paths without depending on DatasetPaths."""
    base = workspace_root / "kb" / kb_name / dataset
    return GraphExtractionPaths(
        rechunk_output_dir=base / "kg_file" / "rechunked",
        graph_dir=base / "kg_file",
        kb_name=kb_name,
        dataset=dataset,
    )


def _count_jsonl_records(input_dir: Path) -> tuple[int, int]:
    if not input_dir.exists():
        return 0, 0
    file_count = 0
    record_count = 0
    for jsonl_file in sorted(input_dir.glob("*.jsonl")):
        file_count += 1
        with jsonl_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    record_count += 1
    return file_count, record_count


def run_graph_extraction_stage(
    paths: GraphExtractionPaths,
    config: GraphExtractionConfig,
) -> GraphExtractionStageResult:
    """Extract raw graph artifacts from rechunked text and optionally persist them."""
    start = time.time()
    logger.info("Loading chunks from %s", paths.rechunk_output_dir)
    text_chunks = load_chunks_from_jsonl(paths.rechunk_output_dir)

    all_relations, all_nodes = batch_extract_kg_from_chunks(
        text_chunks=text_chunks,
        entity_schema_str=config.entity_schema_str,
        relation_schema_str=config.relation_schema_str,
        use_llm=config.use_llm,
        prompts=config.prompts,
        output_dir=paths.graph_dir,
        batch_size=config.batch_size,
        max_workers=config.max_workers,
        show_progress=config.show_progress,
        max_file_size_bytes=config.max_file_size_bytes,
    )

    logger.info(
        "Graph extraction done: %d nodes, %d relations in %.1fs",
        len(all_nodes),
        len(all_relations),
        time.time() - start,
    )

    persisted_raw_graph = False
    if config.persist_raw_graph:
        dataset = f"{paths.kb_name}__{paths.dataset}"
        persist_raw_graph(
            graph_dir=paths.graph_dir,
            dataset=dataset,
            chunk_table=f"{dataset}_chunks",
            embedding_api_key=config.embedding_api_key,
            embedding_url=config.embedding_url,
            embedding_model=config.embedding_model,
            embedding_timeout=config.embedding_timeout,
            embedding_dim=config.embedding_dim,
            embedding_batch_size=config.embedding_batch_size,
            embedding_max_retries=config.embedding_max_retries,
            embedding_retry_delay=config.embedding_retry_delay,
        )
        persisted_raw_graph = True

    node_files, node_count = _count_jsonl_records(paths.graph_dir / "node")
    relation_files, relation_count = _count_jsonl_records(paths.graph_dir / "relation")
    return GraphExtractionStageResult(
        chunk_count=len(text_chunks),
        node_count=node_count,
        relation_count=relation_count,
        node_files=node_files,
        relation_files=relation_files,
        persisted_raw_graph=persisted_raw_graph,
    )
