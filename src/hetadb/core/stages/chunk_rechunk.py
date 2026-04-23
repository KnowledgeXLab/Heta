"""Chunk + rechunk stage for HetaDB pipelines."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from typing import Any

from hetadb.core.db_build.graph_db.chunks_merge import (
    ingest_original_chunks,
    merge_original_chunks,
)
from hetadb.core.db_build.graph_db.text_chunker import chunk_directory, rechunk_by_source
from hetadb.core.db_build.sql_db.sql_db import batch_insert_chunks_pg


logger = logging.getLogger("hetadb.chunk_rechunk")


@dataclass
class ChunkRechunkPaths:
    """Path and identifier model required by the chunk_rechunk stage only."""

    text_json_out: Path
    chunk_dir: Path
    init_file: Path
    merged_chunks_file: Path
    rechunk_output_dir: Path
    chunk_table: str
    chunk_collection: str
    chunk_merge_collection: str


@dataclass
class ChunkRechunkConfig:
    """Minimal config required by the chunk_rechunk stage only."""

    max_batch_bytes: int
    chunk_max_workers: int
    chunk_size: int
    overlap: int
    write_pg: bool
    top_k: int
    nprobe: int
    merge_threshold: float
    max_rounds: int
    num_topk_param: int
    num_threads_param: int
    milvus_host: str
    milvus_port: int
    embedding_batch_size: int
    embedding_num_thread: int
    embedding_api_base: str
    embedding_model: str
    embedding_api_key: str
    embedding_dim: int
    postgres_config: dict[str, Any]
    postgres_batch_size: int
    use_llm: Any
    merge_and_refine_prompt: str
    merge_prompt: str
    stage_target: Literal["ingest", "merge", "rechunk", "persist"] = "persist"


@dataclass
class ChunkRechunkStageResult:
    """Structured outputs produced by the chunk_rechunk stage."""

    stage_target: Literal["ingest", "merge", "rechunk", "persist"]
    reached_stage: Literal["ingest", "merge", "rechunk", "persist"]
    has_original_chunks: bool
    has_init_json: bool
    has_rechunked: bool
    original_chunk_files: int
    rechunked_files: int

    def has_any_output(self) -> bool:
        return self.has_original_chunks or self.has_rechunked


def _build_stage_result(
    paths: ChunkRechunkPaths,
    stage_target: Literal["ingest", "merge", "rechunk", "persist"],
    reached_stage: Literal["ingest", "merge", "rechunk", "persist"],
) -> ChunkRechunkStageResult:
    original_chunk_files = len(list(paths.chunk_dir.glob("chunk_*.jsonl")))
    rechunked_files = len(list(paths.rechunk_output_dir.glob("*.jsonl")))
    return ChunkRechunkStageResult(
        stage_target=stage_target,
        reached_stage=reached_stage,
        has_original_chunks=original_chunk_files > 0,
        has_init_json=paths.init_file.exists(),
        has_rechunked=rechunked_files > 0,
        original_chunk_files=original_chunk_files,
        rechunked_files=rechunked_files,
    )


def build_chunk_rechunk_paths(workspace_root: Path, kb_name: str, dataset: str) -> ChunkRechunkPaths:
    """Build chunk_rechunk-stage paths without depending on DatasetPaths."""
    base = workspace_root / "kb" / kb_name / dataset
    prefix = f"{kb_name}__{dataset}"
    chunk_dir = base / "kg_file" / "original_chunk"
    return ChunkRechunkPaths(
        text_json_out=base / "parsed_file" / "text_json_out",
        chunk_dir=chunk_dir,
        init_file=chunk_dir / "init.json",
        merged_chunks_file=chunk_dir / "merged_chunks.jsonl",
        rechunk_output_dir=base / "kg_file" / "rechunked",
        chunk_table=f"{prefix}_chunks",
        chunk_collection=f"{prefix}_chunk_collection",
        chunk_merge_collection=f"{prefix}_merge_chunk_collection",
    )


def _persist_rechunked_chunks(paths: ChunkRechunkPaths, config: ChunkRechunkConfig) -> int:
    inserted = 0
    for rechunk_file in paths.rechunk_output_dir.glob("*.jsonl"):
        rechunked_chunks = []
        with open(rechunk_file, "r", encoding="utf-8") as f:
            for line in f:
                chunk_data = json.loads(line.strip())
                if "chunk_id" not in chunk_data or "text" not in chunk_data:
                    continue
                source = chunk_data.get("source", "")
                rechunked_chunks.append({
                    "chunk_id": chunk_data["chunk_id"],
                    "text": chunk_data["text"],
                    "source": source,
                    "source_chunk": json.dumps(
                        chunk_data.get("source_chunk", [chunk_data["chunk_id"]]),
                    ),
                })

        if rechunked_chunks:
            batch_insert_chunks_pg(
                chunks_data=rechunked_chunks,
                postgres_config=config.postgres_config,
                chunk_table=paths.chunk_table,
                postgres_batch_size=config.postgres_batch_size,
            )
            inserted += len(rechunked_chunks)
            logger.info(
                "Inserted %d rechunked chunks from %s into %s",
                len(rechunked_chunks),
                rechunk_file.name,
                paths.chunk_table,
            )
    if inserted:
        logger.info("Inserted %d rechunked chunks total", inserted)
    return inserted


def run_chunk_rechunk_stage(
    paths: ChunkRechunkPaths,
    config: ChunkRechunkConfig,
) -> ChunkRechunkStageResult:
    """Generate original chunks, ingest them, merge them, then rechunk by source."""

    chunk_directory(
        input_dir=paths.text_json_out,
        output_dir=paths.chunk_dir,
        max_batch_bytes=config.max_batch_bytes,
        max_workers=config.chunk_max_workers,
        chunk_size=config.chunk_size,
        overlap=config.overlap,
    )

    ingest_original_chunks(
        data_dir=str(paths.chunk_dir),
        chunk_table=paths.chunk_table,
        write_pg=config.write_pg,
        milvus_collections=[paths.chunk_collection, paths.chunk_merge_collection],
        embedding_batch_size=config.embedding_batch_size,
        embedding_num_thread=config.embedding_num_thread,
        embedding_api_base=config.embedding_api_base,
        embedding_model=config.embedding_model,
        embedding_api_key=config.embedding_api_key,
        embedding_dim=config.embedding_dim,
        postgres_config=config.postgres_config,
        postgres_batch_size=config.postgres_batch_size,
    )
    if config.stage_target == "ingest":
        return _build_stage_result(paths, config.stage_target, "ingest")

    merge_original_chunks(
        chunks_path=str(paths.chunk_dir),
        collection_name=paths.chunk_merge_collection,
        top_k=config.top_k,
        nprobe=config.nprobe,
        merge_threshold=config.merge_threshold,
        max_rounds=config.max_rounds,
        num_topk_param=config.num_topk_param,
        num_threads_param=config.num_threads_param,
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        target_merge_collection=paths.chunk_merge_collection,
        embedding_api_base=config.embedding_api_base,
        embedding_model=config.embedding_model,
        embedding_api_key=config.embedding_api_key,
        embedding_dim=config.embedding_dim,
        use_llm=config.use_llm,
        merge_and_refine_prompt=config.merge_and_refine_prompt,
        merge_prompt=config.merge_prompt,
        merged_chunks_file=str(paths.merged_chunks_file),
    )
    if config.stage_target == "merge":
        return _build_stage_result(paths, config.stage_target, "merge")

    rechunk_by_source(
        chunk_dir=paths.chunk_dir,
        output_dir=paths.rechunk_output_dir,
        chunk_size=config.chunk_size,
        overlap=config.overlap,
    )
    if config.stage_target == "rechunk":
        return _build_stage_result(paths, config.stage_target, "rechunk")

    _persist_rechunked_chunks(paths, config)
    return _build_stage_result(paths, config.stage_target, "persist")
