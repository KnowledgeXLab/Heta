"""File processor for HetaDB.

Orchestrates document processing: file parsing -> chunking -> graph extraction
-> node/relation dedup & merge -> relation export -> table embedding.
"""

import json
import shutil
import threading
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from common.config import get_persistence
from common.llm_client import create_use_llm, create_use_llm_async, create_use_vlm
from hetadb.utils.path import PROJECT_ROOT, PACKAGE_ROOT
from hetadb.utils.schema import load_workspace_schema
from hetadb.core.db_build.graph_db.graph_vector import embedding
from hetadb.core.db_build.sql_db.csv_ingestor import AutoSchemaCSVIngestor
from hetadb.core.db_build.vector_db.vector_db import (
    ensure_nodes_collection,
    insert_nodes_records_to_milvus,
)
from hetadb.core.stages.chunk_rechunk import (
    ChunkRechunkConfig,
    build_chunk_rechunk_paths,
    run_chunk_rechunk_stage,
)
from hetadb.core.stages.graph_dedup import (
    GraphDedupConfig,
    build_graph_dedup_paths,
    run_graph_dedup_stage,
)
from hetadb.core.stages.graph_extraction import (
    GraphExtractionConfig,
    build_graph_extraction_paths,
    run_graph_extraction_stage,
)
from hetadb.core.stages.parse import ParseConfig, build_parse_paths, run_parse_stage


logger = logging.getLogger("hetadb.file_processor")


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 120
    max_concurrent_requests: int = 10
    max_retries: int = 3


@dataclass
class VLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 120
    max_concurrent_requests: int = 10
    max_retries: int = 3


@dataclass
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    dim: int = 1024
    batch_size: int = 2000
    num_threads: int = 8
    timeout: int = 30
    max_file_size_bytes: int = 3221225472
    max_retries: int = 5
    retry_delay: int = 2


@dataclass
class DatabaseConfig:
    postgres_config: dict[str, Any] = field(default_factory=dict)
    postgres_batch_size: int = 500
    milvus_config: dict[str, Any] = field(default_factory=dict)
    milvus_host: str = "127.0.0.1"
    milvus_port: int = 19530


@dataclass
class ParseStageSettings:
    max_workers: int = 4
    supported_ext: str | set[str] = "default"


@dataclass
class ChunkRechunkStageSettings:
    chunk_size: int
    overlap: int
    max_batch_bytes: int
    max_workers: int
    top_k: int
    nprobe: int
    merge_threshold: float
    max_rounds: int
    num_topk_param: int


@dataclass
class GraphConfig:
    batch_size: int
    max_workers: int
    max_file_size_bytes: int
    persist_raw_graph: bool = False
    entity_schema_csv_path: str | None = None
    relation_schema_csv_path: str | None = None
    entity_schema_str: str = ""
    relation_schema_str: str = ""


@dataclass
class GraphDedupStageConfig:
    parallel_batches: int
    batch_size: int
    llm_batch_size: int
    max_rounds: int
    milvus_dedup_batch_size: int
    llm_max_retries: int
    node_top_k: int
    node_sim_threshold: float
    node_temperature: float
    rel_top_k: int
    rel_sim_threshold: float
    rel_temperature: float


@dataclass
class PromptConfig:
    entity_template: str = ""
    relation_template: str = ""
    node_prompt: str = ""
    rel_prompt: str = ""
    merge_and_refine_prompt: str = ""
    merge_prompt: str = ""
    dedup_template: str = ""
    merge_cluster_prompt: str = ""
    dedup_rel_template: str = ""
    merge_rel_prompt: str = ""


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager:
    """Load and cache all processing config from project-level config.yaml
    and package-level db_config / prompt files."""

    def __init__(self):
        self._project_cfg: dict | None = None
        self._db_cfg: dict | None = None
        self._prompt_cfg: dict | None = None

    def _load_project_config(self) -> dict:
        if self._project_cfg is None:
            with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
                self._project_cfg = yaml.safe_load(f).get("hetadb", {})
        return self._project_cfg

    def _load_db_config(self) -> dict:
        if self._db_cfg is None:
            path = PACKAGE_ROOT / "config" / "db_config.yaml"
            with open(path, encoding="utf-8") as f:
                self._db_cfg = yaml.safe_load(f)
        return self._db_cfg

    @staticmethod
    def _require(d: dict, key: str, section: str):
        """Retrieve a required field from a config dict, raising on absence."""
        if key not in d:
            path = PACKAGE_ROOT / "config" / "db_config.yaml"
            raise ValueError(
                f"Missing required field '{section}.{key}' in config file: {path}"
            )
        return d[key]

    def _load_prompt_config(self) -> dict:
        if self._prompt_cfg is None:
            path = PACKAGE_ROOT / "config" / "prompt" / "kg_prompt.yaml"
            with open(path, encoding="utf-8") as f:
                self._prompt_cfg = yaml.safe_load(f)
        return self._prompt_cfg

    def get_workspace_root(self) -> Path:
        """Resolve the workspace root from config. Supports absolute and relative paths."""
        workspace = self._load_project_config().get("workspace", "workspace")
        p = Path(workspace)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def get_llm_config(self) -> LLMConfig:
        cfg = self._load_project_config()["llm"]
        return LLMConfig(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            timeout=cfg.get("timeout", 120),
            max_concurrent_requests=cfg.get("max_concurrent_requests", 10),
            max_retries=cfg.get("max_retries", 3),
        )

    def get_vlm_config(self) -> VLMConfig:
        cfg = self._load_project_config()["vlm"]
        return VLMConfig(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            timeout=cfg.get("timeout", 120),
            max_concurrent_requests=cfg.get("max_concurrent_requests", 10),
            max_retries=cfg.get("max_retries", 3),
        )

    def get_embedding_config(self) -> EmbeddingConfig:
        cfg = self._load_project_config()["embedding_api"]
        return EmbeddingConfig(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            dim=cfg.get("dim", 1024),
            batch_size=cfg.get("batch_size", 2000),
            num_threads=cfg.get("num_threads", 8),
            timeout=cfg.get("timeout", 30),
            max_file_size_bytes=cfg.get("max_file_size_bytes", 3221225472),
            max_retries=cfg.get("max_retries", 5),
            retry_delay=cfg.get("retry_delay", 2),
        )

    def get_database_config(self) -> DatabaseConfig:
        mv_local = self._load_project_config().get("milvus", {})
        pg = get_persistence("postgresql")
        mv = get_persistence("milvus")
        db_param = self._load_db_config()
        return DatabaseConfig(
            postgres_config=pg,
            postgres_batch_size=db_param.get("postgres_batch_size", 500),
            milvus_config={**mv, **mv_local},
            milvus_host=mv.get("host", "127.0.0.1"),
            milvus_port=int(mv.get("port", 19530)),
        )

    def get_parse_stage_settings(self) -> ParseStageSettings:
        db = self._load_db_config()
        req = self._require
        param = db["parameter"]
        parse_cfg = req(param, "parse", "parameter")
        supported_ext = req(parse_cfg, "supported_ext", "parameter.parse")
        if isinstance(supported_ext, list):
            supported_ext = {str(ext) for ext in supported_ext}
        return ParseStageSettings(
            max_workers=req(parse_cfg, "max_workers", "parameter.parse"),
            supported_ext=supported_ext,
        )

    def get_chunk_rechunk_settings(self) -> ChunkRechunkStageSettings:
        db = self._load_db_config()
        req = self._require
        param = db["parameter"]
        chunk = req(param, "chunk_rechunk", "parameter")
        return ChunkRechunkStageSettings(
            chunk_size=req(chunk, "chunk_size", "parameter.chunk_rechunk"),
            overlap=req(chunk, "overlap", "parameter.chunk_rechunk"),
            max_batch_bytes=req(chunk, "max_batch_bytes", "parameter.chunk_rechunk"),
            max_workers=req(chunk, "max_workers", "parameter.chunk_rechunk"),
            top_k=req(chunk, "top_k", "parameter.chunk_rechunk"),
            nprobe=req(chunk, "nprobe", "parameter.chunk_rechunk"),
            merge_threshold=req(chunk, "merge_threshold", "parameter.chunk_rechunk"),
            max_rounds=req(chunk, "max_rounds", "parameter.chunk_rechunk"),
            num_topk_param=req(chunk, "num_topk_param", "parameter.chunk_rechunk"),
        )

    def get_graph_config(self) -> GraphConfig:
        db = self._load_db_config()
        req = self._require
        param = db["parameter"]
        graph = req(param, "graph_extraction", "parameter")
        return GraphConfig(
            batch_size=req(graph, "batch_size", "parameter.graph_extraction"),
            max_workers=req(graph, "max_workers", "parameter.graph_extraction"),
            max_file_size_bytes=req(graph, "max_file_size_bytes", "parameter.graph_extraction"),
            entity_schema_csv_path=graph.get("entity_schema_csv_path") or None,
            relation_schema_csv_path=graph.get("relation_schema_csv_path") or None,
            persist_raw_graph=graph.get("persist_raw_graph", False),
        )

    def get_graph_dedup_config(self) -> GraphDedupStageConfig:
        db = self._load_db_config()
        req = self._require
        param = db["parameter"]
        dedup = req(param, "graph_dedup", "parameter")
        node_merge = req(dedup, "node_merge", "parameter.graph_dedup")
        rel_merge = req(dedup, "rel_merge", "parameter.graph_dedup")
        return GraphDedupStageConfig(
            parallel_batches=req(dedup, "parallel_batches", "parameter.graph_dedup"),
            batch_size=req(dedup, "batch_size", "parameter.graph_dedup"),
            llm_batch_size=req(dedup, "llm_batch_size", "parameter.graph_dedup"),
            max_rounds=req(dedup, "max_rounds", "parameter.graph_dedup"),
            milvus_dedup_batch_size=req(dedup, "milvus_dedup_batch_size", "parameter.graph_dedup"),
            llm_max_retries=req(dedup, "llm_max_retries", "parameter.graph_dedup"),
            node_top_k=req(node_merge, "top_k", "parameter.graph_dedup.node_merge"),
            node_sim_threshold=req(node_merge, "sim_threshold", "parameter.graph_dedup.node_merge"),
            node_temperature=req(node_merge, "temperature", "parameter.graph_dedup.node_merge"),
            rel_top_k=req(rel_merge, "top_k", "parameter.graph_dedup.rel_merge"),
            rel_sim_threshold=req(rel_merge, "sim_threshold", "parameter.graph_dedup.rel_merge"),
            rel_temperature=req(rel_merge, "temperature", "parameter.graph_dedup.rel_merge"),
        )

    def get_parse_max_workers(self) -> int:
        """Return the max number of concurrent dataset parse tasks.

        Reads from ``hetadb.parse_max_workers`` in ``config.yaml`` (user-facing),
        falling back to ``parse_max_workers`` in ``db_config.yaml``, then 2.
        """
        project_val = self._load_project_config().get("parse_max_workers")
        if project_val is not None:
            return int(project_val)
        return self._load_db_config().get("parse_max_workers", 2)

    def get_prompt_config(self) -> PromptConfig:
        p = self._load_prompt_config()
        return PromptConfig(
            entity_template=p["entity_template"],
            relation_template=p["relation_template"],
            node_prompt=p["node_prompt"],
            rel_prompt=p["rel_prompt"],
            merge_and_refine_prompt=p["chunk_merge_refine_prompt"],
            merge_prompt=p["chunk_merge_prompt"],
            dedup_template=p["dedup_node_template"],
            merge_cluster_prompt=p["merge_node_cluster_prompt"],
            dedup_rel_template=p["dedup_rel_template"],
            merge_rel_prompt=p["merge_rel_prompt"],
        )


# ---------------------------------------------------------------------------
# ProcessorConfig
# ---------------------------------------------------------------------------

@dataclass
class ProcessorConfig:
    """Aggregated processing config with pre-built LLM clients."""
    llm_config: LLMConfig
    vlm_config: VLMConfig
    embedding_config: EmbeddingConfig
    database_config: DatabaseConfig
    parse_stage_settings: ParseStageSettings
    chunk_rechunk_settings: ChunkRechunkStageSettings
    graph_config: GraphConfig
    graph_dedup_config: GraphDedupStageConfig
    prompt_config: PromptConfig

    llm_client: Any = None
    vlm_client: Any = None
    use_llm_fn: Any = None
    embedding_cfg: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.llm_client = create_use_llm_async(
            url=self.llm_config.base_url,
            api_key=self.llm_config.api_key,
            model=self.llm_config.model,
            timeout=self.llm_config.timeout,
            max_retries=self.llm_config.max_retries,
            max_concurrent_requests=self.llm_config.max_concurrent_requests,
        )
        self.vlm_client = create_use_vlm(
            url=self.vlm_config.base_url,
            api_key=self.vlm_config.api_key,
            model=self.vlm_config.model,
            timeout=self.vlm_config.timeout,
            max_retries=self.vlm_config.max_retries,
            max_concurrent_requests=self.vlm_config.max_concurrent_requests,
        )
        self.use_llm_fn = create_use_llm(
            url=self.llm_config.base_url,
            api_key=self.llm_config.api_key,
            model=self.llm_config.model,
            timeout=self.llm_config.timeout,
            max_retries=self.llm_config.max_retries,
        )
        self.embedding_cfg = {
            "api_key": self.embedding_config.api_key,
            "embedding_url": self.embedding_config.base_url,
            "embedding_model": self.embedding_config.model,
            "embedding_timeout": self.embedding_config.timeout,
        }


def create_processor_config() -> ProcessorConfig:
    """Build a complete ProcessorConfig from project config files."""
    mgr = ConfigManager()
    return ProcessorConfig(
        llm_config=mgr.get_llm_config(),
        vlm_config=mgr.get_vlm_config(),
        embedding_config=mgr.get_embedding_config(),
        database_config=mgr.get_database_config(),
        parse_stage_settings=mgr.get_parse_stage_settings(),
        chunk_rechunk_settings=mgr.get_chunk_rechunk_settings(),
        graph_config=mgr.get_graph_config(),
        graph_dedup_config=mgr.get_graph_dedup_config(),
        prompt_config=mgr.get_prompt_config(),
    )


# ---------------------------------------------------------------------------
# DatasetPaths
# ---------------------------------------------------------------------------

@dataclass
class DatasetPaths:
    """All resolved paths and DB identifiers for a dataset being processed into a KB."""
    workspace_root: Path
    kb_name: str
    dataset: str

    def __post_init__(self):
        # Raw source files live in workspace/raw_files/{dataset}/
        self.raw_files_dir: Path = self.workspace_root / "raw_files" / self.dataset

        # Processed artifacts live in workspace/kb/{kb_name}/{dataset}/
        base: Path = self.workspace_root / "kb" / self.kb_name / self.dataset

        # Parsed output
        self.text_json_out: Path = base / "parsed_file" / "text_json_out"

        # Chunk output consumed by downstream graph extraction.
        self.rechunk_output_dir: Path = base / "kg_file" / "rechunked"

        # Graph
        self.graph_dir: Path = base / "kg_file"

        # CSV / table
        self.csv_dir: Path = base / "parsed_file" / "csv_out"
        self.table_desc_dir: Path = base / "parsed_file" / "table_desc_out"
        self.table_info_dir: Path = base / "parsed_file" / "table_info"
        self.kg_node_dir: Path = base / "kg_file" / "table"

        # Node paths
        self.original_kg_node_input_path: Path = base / "kg_file" / "node" / "nodes_0000.jsonl"
        self.dedup_kg_node_output_path: Path = base / "kg_file" / "dedup" / "dedup_node.jsonl"
        self.dedup_kg_node_embedding_output_path: Path = base / "kg_file" / "dedup_node_emb"
        self.batch_kg_node_merge_output_path: Path = base / "kg_file" / "batch_merge_nodes"
        self.final_kg_node_merge_output_path: Path = base / "kg_file" / "final_nodes"
        self.mapping_path: Path = base / "kg_file" / "final_nodes" / "final_mapping.json"

        # Relation paths
        self.original_kg_rel_input_path: Path = base / "kg_file" / "relation" / "relations_0000.jsonl"
        self.dedup_kg_rel_output_path: Path = base / "kg_file" / "dedup_rel" / "dedup_rel.jsonl"
        self.dedup_kg_rel_embedding_output_path: Path = base / "kg_file" / "dedup_rel_emb"
        self.batch_kg_rel_merge_output_path: Path = base / "kg_file" / "batch_merge_rels"
        self.final_kg_rel_merge_output_path: Path = base / "kg_file" / "final_res"

        # Meta file written on processing completion
        self.meta_path: Path = base / "_meta.json"

# ---------------------------------------------------------------------------
# Processing stages
# ---------------------------------------------------------------------------

_processor_config: ProcessorConfig | None = None


def _get_processor_config() -> ProcessorConfig:
    global _processor_config
    if _processor_config is None:
        _processor_config = create_processor_config()
    return _processor_config


def run_chunk_processing(paths: DatasetPaths, config: ProcessorConfig) -> None:
    """Split parsed text into chunks, merge similar chunks, rechunk, and insert into DB."""
    run_chunk_rechunk_stage(
        build_chunk_rechunk_paths(paths.workspace_root, paths.kb_name, paths.dataset),
        ChunkRechunkConfig(
            max_batch_bytes=config.chunk_rechunk_settings.max_batch_bytes,
            chunk_max_workers=config.chunk_rechunk_settings.max_workers,
            chunk_size=config.chunk_rechunk_settings.chunk_size,
            overlap=config.chunk_rechunk_settings.overlap,
            write_pg=True,
            top_k=config.chunk_rechunk_settings.top_k,
            nprobe=config.chunk_rechunk_settings.nprobe,
            merge_threshold=config.chunk_rechunk_settings.merge_threshold,
            max_rounds=config.chunk_rechunk_settings.max_rounds,
            num_topk_param=config.chunk_rechunk_settings.num_topk_param,
            num_threads_param=config.llm_config.max_concurrent_requests,
            milvus_host=config.database_config.milvus_host,
            milvus_port=config.database_config.milvus_port,
            embedding_batch_size=config.embedding_config.batch_size,
            embedding_num_thread=config.embedding_config.num_threads,
            embedding_api_base=config.embedding_config.base_url,
            embedding_model=config.embedding_config.model,
            embedding_api_key=config.embedding_config.api_key,
            embedding_dim=config.embedding_config.dim,
            postgres_config=config.database_config.postgres_config,
            postgres_batch_size=config.database_config.postgres_batch_size,
            use_llm=config.use_llm_fn,
            merge_and_refine_prompt=config.prompt_config.merge_and_refine_prompt,
            merge_prompt=config.prompt_config.merge_prompt,
            stage_target="persist",
        ),
    )


def run_graph_extraction(
    paths: DatasetPaths,
    config: ProcessorConfig,
    entity_schema_str: str = "",
) -> None:
    """Extract knowledge graph (entities + relations) from rechunked text via LLM."""
    run_graph_extraction_stage(
        paths=build_graph_extraction_paths(paths.workspace_root, paths.kb_name, paths.dataset),
        config=GraphExtractionConfig(
            entity_schema_str=entity_schema_str or config.graph_config.entity_schema_str,
            relation_schema_str=config.graph_config.relation_schema_str,
            use_llm=config.use_llm_fn,
            prompts={
                "entity_template": config.prompt_config.entity_template,
                "relation_template": config.prompt_config.relation_template,
                "node_prompt": config.prompt_config.node_prompt,
                "rel_prompt": config.prompt_config.rel_prompt,
                "chunk_merge_refine_prompt": config.prompt_config.merge_and_refine_prompt,
                "chunk_merge_prompt": config.prompt_config.merge_prompt,
                "dedup_node_template": config.prompt_config.dedup_template,
                "merge_node_cluster_prompt": config.prompt_config.merge_cluster_prompt,
                "dedup_rel_template": config.prompt_config.dedup_rel_template,
                "merge_rel_prompt": config.prompt_config.merge_rel_prompt,
            },
            batch_size=config.graph_config.batch_size,
            max_workers=config.graph_config.max_workers,
            max_file_size_bytes=config.graph_config.max_file_size_bytes,
            show_progress=True,
            persist_raw_graph=config.graph_config.persist_raw_graph,
            embedding_api_key=config.embedding_config.api_key,
            embedding_url=config.embedding_config.base_url,
            embedding_model=config.embedding_config.model,
            embedding_timeout=config.embedding_config.timeout,
            embedding_dim=config.embedding_config.dim,
            embedding_batch_size=config.embedding_config.batch_size,
            embedding_max_retries=config.embedding_config.max_retries,
            embedding_retry_delay=config.embedding_config.retry_delay,
        ),
    )


def run_table_embedding(paths: DatasetPaths, config: ProcessorConfig) -> None:
    """Generate table nodes from CSV files and insert embeddings into Milvus."""
    start = time.time()

    # 1. Generate table nodes via CSV ingestion (sync LLM client avoids event-loop
    #    conflicts when called from a background thread via ThreadPoolExecutor).
    ingestor = AutoSchemaCSVIngestor(
        csv_dir=str(paths.csv_dir),
        table_desc_dir=str(paths.table_desc_dir),
        table_info_dir=str(paths.table_info_dir),
        kg_node_dir=str(paths.kg_node_dir),
        postgres_config=config.database_config.postgres_config.copy(),
        use_llm=config.use_llm_fn,
    )
    ingestor.run()
    logger.info("Table nodes generated successfully")

    # 2. Load generated table nodes
    table_node_file = paths.kg_node_dir / "table_node.jsonl"
    if not table_node_file.exists():
        logger.warning("table_node.jsonl not found at %s, skipping", table_node_file)
        return

    nodes = []
    with open(table_node_file, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                nodes.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("Invalid JSON on line %d: %s", line_num, e)

    if not nodes:
        return
    logger.info("Loaded %d table nodes", len(nodes))

    # 3. Generate embeddings
    descriptions = [node.get("Description", "") for node in nodes]
    embeddings = embedding(
        texts=descriptions,
        api_key=config.embedding_config.api_key,
        embedding_url=config.embedding_config.base_url,
        embedding_model=config.embedding_config.model,
        embedding_timeout=config.embedding_config.timeout,
    )

    # 4. Insert into Milvus
    excluded_keys = {"Id", "NodeName", "Description", "Type", "SubType", "Embedding"}
    records = [
        {
            "id": node.get("Id", f"node_{i}"),
            "nodename": node.get("NodeName", ""),
            "description": node.get("Description", ""),
            "type": node.get("Type", ""),
            "subtype": node.get("SubType", ""),
            "attr": json.dumps(
                {k: v for k, v in node.items() if k not in excluded_keys},
                ensure_ascii=False,
            ),
            "embedding": emb_vec,
        }
        for i, (node, emb_vec) in enumerate(zip(nodes, embeddings))
    ]

    collection_name = f"{paths.kb_name}__{paths.dataset}_entity_collection"
    collection = ensure_nodes_collection(collection_name, dim=config.embedding_config.dim)
    insert_nodes_records_to_milvus(collection, records)
    logger.info(
        "Inserted %d table node records into %s in %.1fs",
        len(records), collection_name, time.time() - start,
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

def _clean_dataset(workspace_root: Path, kb_name: str, dataset: str) -> None:
    """Remove all prior artifacts for a dataset before re-parsing.

    Order matters:
      1. Read table_info/ to discover CSV-derived PG table names BEFORE
         deleting the directory.
      2. Drop Milvus collections and all PG tables (standard + CSV-derived).
      3. Delete parsed_file/, kg_file/, and _meta.json.

    DB cleanup errors are logged as warnings and do not abort the pipeline.
    """
    base = workspace_root / "kb" / kb_name / dataset
    prefix = f"{kb_name}__{dataset}"

    # Collect CSV-derived table names from table_info/*.json before deletion.
    # The filename (stem) is the PG table name created by csv_ingestor.
    csv_tables: list[str] = []
    table_info_dir = base / "parsed_file" / "table_info"
    if table_info_dir.exists():
        csv_tables = [p.stem for p in table_info_dir.glob("*.json")]
        if csv_tables:
            logger.info(
                "Found %d CSV-derived table(s) to drop for %s/%s: %s",
                len(csv_tables), kb_name, dataset, csv_tables,
            )

    # Drop Milvus collections and PG tables.
    try:
        from pymilvus import utility
        from hetadb.core.db_build.vector_db.vector_db import connect_milvus
        from hetadb.core.db_build.sql_db.sql_db import drop_dataset_tables
        from hetadb.utils.load_config import get_postgres_conn_config
        import psycopg2

        connect_milvus()
        for suffix in (
            "_chunk_collection",
            "_merge_chunk_collection",
            "_entity_collection",
            "_relation_collection",
            "_node_dedup_collection",
            "_rel_dedup_collection",
        ):
            name = f"{prefix}{suffix}"
            if utility.has_collection(name):
                utility.drop_collection(name)
                logger.info("Dropped Milvus collection: %s", name)

        try:
            drop_dataset_tables(prefix)
        except Exception as e:
            logger.warning("Failed to drop standard PG tables for %s: %s", prefix, e)

        # Drop CSV-derived tables (named after csv_caption, no dataset prefix).
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
                logger.warning("Failed to drop CSV-derived PG tables for %s: %s", prefix, e)

    except Exception as e:
        logger.warning("DB cleanup failed for %s: %s", prefix, e)

    # Delete the entire dataset directory last (table_info already read above).
    # Removing the whole directory — not just subdirs — ensures no empty shell
    # is left behind that would make the dataset appear as "Not parsed" in the
    # KB listing.  The pipeline recreates the directory on the next run.
    if base.exists():
        shutil.rmtree(base)
        logger.info("Removed dataset directory %s/%s", kb_name, dataset)


def _write_dataset_meta(paths: DatasetPaths, process_mode: int) -> None:
    """Write _meta.json for a successfully processed dataset."""
    paths.meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "process_mode": process_mode,
        "parsed_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(paths.meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info("Written _meta.json for %s/%s", paths.kb_name, paths.dataset)


def _run_mode0_pipeline(
    task_id: str,
    workspace_root: Path,
    kb_name: str,
    dataset: str,
    schema_name: str | None = None,
    cancel_token: "threading.Event | None" = None,
) -> None:
    """Mode 0 pipeline: parse → chunk → graph → dedup → embed.

    cancel_token: when set by an external cancel request, the pipeline stops
    at the next stage boundary, rolls back, and raises CancelledError.
    """
    from common.tasks import update_task, TaskStatus

    config = _get_processor_config()
    paths = DatasetPaths(workspace_root=workspace_root, kb_name=kb_name, dataset=dataset)

    # Resolve custom entity schema without mutating the shared cached config.
    entity_schema_str = ""
    if schema_name:
        entity_schema_str = load_workspace_schema(workspace_root, schema_name)
        if entity_schema_str:
            logger.info("Using custom entity schema '%s' for %s/%s", schema_name, kb_name, dataset)
        else:
            logger.warning("Schema '%s' not found; falling back to default entity template", schema_name)

    stages = [
        (
            0.10,
            "file parsing",
            lambda: run_parse_stage(
                build_parse_paths(workspace_root, kb_name, dataset),
                ParseConfig(
                    llm_client=config.llm_client,
                    vlm_client=config.vlm_client,
                    max_workers=config.parse_stage_settings.max_workers,
                    supported_ext=config.parse_stage_settings.supported_ext,
                ),
            ),
        ),
        (0.25, "chunk processing",   lambda: run_chunk_processing(paths, config)),
        (0.40, "graph extraction",   lambda: run_graph_extraction(paths, config, entity_schema_str)),
        (
            0.55,
            "graph dedup",
            lambda: run_graph_dedup_stage(
                build_graph_dedup_paths(workspace_root, kb_name, dataset),
                GraphDedupConfig(
                    use_llm=config.use_llm_fn,
                    embedding_cfg=config.embedding_cfg,
                    llm_max_concurrent_requests=config.llm_config.max_concurrent_requests,
                    merge_max_rounds=config.graph_dedup_config.max_rounds,
                    merge_llm_batch_size=config.graph_dedup_config.llm_batch_size,
                    merge_batch_size=config.graph_dedup_config.batch_size,
                    merge_parallel_batches=config.graph_dedup_config.parallel_batches,
                    merge_llm_max_retries=config.graph_dedup_config.llm_max_retries,
                    merge_milvus_dedup_batch_size=config.graph_dedup_config.milvus_dedup_batch_size,
                    merge_node_top_k=config.graph_dedup_config.node_top_k,
                    merge_node_sim_threshold=config.graph_dedup_config.node_sim_threshold,
                    merge_node_temperature=config.graph_dedup_config.node_temperature,
                    merge_rel_top_k=config.graph_dedup_config.rel_top_k,
                    merge_rel_sim_threshold=config.graph_dedup_config.rel_sim_threshold,
                    merge_rel_temperature=config.graph_dedup_config.rel_temperature,
                    embedding_api_key=config.embedding_config.api_key,
                    embedding_url=config.embedding_config.base_url,
                    embedding_model=config.embedding_config.model,
                    embedding_timeout=config.embedding_config.timeout,
                    embedding_batch_size=config.embedding_config.batch_size,
                    embedding_max_file_size_bytes=config.embedding_config.max_file_size_bytes,
                    embedding_num_threads=config.embedding_config.num_threads,
                    embedding_max_retries=config.embedding_config.max_retries,
                    embedding_retry_delay=config.embedding_config.retry_delay,
                    embedding_dim=config.embedding_config.dim,
                    dedup_template=config.prompt_config.dedup_template,
                    merge_cluster_prompt=config.prompt_config.merge_cluster_prompt,
                    dedup_rel_template=config.prompt_config.dedup_rel_template,
                    merge_rel_prompt=config.prompt_config.merge_rel_prompt,
                ),
            ),
        ),
        (0.95, "table embedding",    lambda: run_table_embedding(paths, config)),
    ]

    parse_result = None
    for progress, stage_name, stage_func in stages:
        # Check for cancellation before starting each stage.
        if cancel_token is not None and cancel_token.is_set():
            logger.info("Task %s cancelled before stage '%s' — rolling back", task_id, stage_name)
            _clean_dataset(workspace_root, kb_name, dataset)
            update_task(task_id, status=TaskStatus.CANCELLED, message="Cancelled by user")
            return

        update_task(task_id, progress=progress, message=f"Running {stage_name}...")
        stage_output = stage_func()
        if stage_name == "file parsing":
            parse_result = stage_output

        # After file parsing, stop early only when neither text nor table output
        # was produced.  Table-only datasets write to csv_out (not text_json_out),
        # so checking text_json_out alone would incorrectly skip run_table_embedding.
        if stage_name == "file parsing":
            if parse_result is None or not parse_result.has_any_output():
                logger.warning("No files parsed for %s/%s — skipping remaining stages", kb_name, dataset)
                update_task(task_id, status=TaskStatus.FAILED, message="No files could be parsed")
                return

    # Final check after the last stage completes.
    if cancel_token is not None and cancel_token.is_set():
        logger.info("Task %s cancelled after last stage — rolling back", task_id)
        _clean_dataset(workspace_root, kb_name, dataset)
        update_task(task_id, status=TaskStatus.CANCELLED, message="Cancelled by user")
        return


_SUPPORTED_MODES = {0}


def run_file_processing(
    task_id: str,
    workspace_root: Path,
    kb_name: str,
    dataset: str,
    mode: int = 0,
    schema_name: str | None = None,
    cancel_token: "threading.Event | None" = None,
) -> None:
    """Run document processing as a background task.

    On success, writes workspace/kb/{kb_name}/{dataset}/_meta.json.

    Args:
        schema_name: Name of a custom entity schema stored in workspace/schemas/.
            When provided, overrides the default entity extraction schema for the
            graph extraction stage only.
        cancel_token: threading.Event supplied by the task store.  When set,
            the pipeline stops at the next stage boundary, rolls back, and
            marks the task CANCELLED.
    """
    import traceback
    from common.tasks import TaskStatus, update_task

    # Honour a cancel that arrived while the task was still PENDING in the queue.
    # Without this check, executor.submit() would run the task anyway and
    # overwrite the CANCELLED status set by cancel_task().
    if cancel_token is not None and cancel_token.is_set():
        update_task(task_id, status=TaskStatus.CANCELLED, message="Cancelled before start")
        return

    try:
        if mode not in _SUPPORTED_MODES:
            raise ValueError(f"Unsupported processing mode: {mode}")

        update_task(task_id, status=TaskStatus.RUNNING, message="Cleaning up previous data...")
        _clean_dataset(workspace_root, kb_name, dataset)

        update_task(task_id, progress=0.05, message="Initializing...")
        if mode == 0:
            _run_mode0_pipeline(
                task_id, workspace_root, kb_name, dataset, schema_name,
                cancel_token=cancel_token,
            )

        # If the pipeline was cancelled or failed early (e.g. no parseable files),
        # the task status is already set — skip writing metadata and marking COMPLETED.
        from common.tasks import get_task
        task = get_task(task_id)
        if task and task.status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
            return

        paths = DatasetPaths(workspace_root=workspace_root, kb_name=kb_name, dataset=dataset)
        _write_dataset_meta(paths, mode)

        update_task(task_id, status=TaskStatus.COMPLETED, progress=1.0, message="Processing completed")

    except Exception as e:
        logger.error("Processing task %s failed: %s\n%s", task_id, e, traceback.format_exc())
        # Atomic rollback: remove any partial state so the dataset is left clean.
        try:
            _clean_dataset(workspace_root, kb_name, dataset)
            logger.info("Rolled back partial state for %s/%s", kb_name, dataset)
        except Exception as cleanup_err:
            logger.warning(
                "Rollback cleanup failed for %s/%s: %s", kb_name, dataset, cleanup_err,
            )
        update_task(task_id, status=TaskStatus.FAILED, error=str(e), message="Processing failed")
