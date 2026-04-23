"""LLM-based knowledge graph extraction from text chunks."""

import csv
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TextIO

from hetadb.utils.hash_filename import get_sha256_hash
from common.llm_client import parse_nodes

logger = logging.getLogger(__name__)


def _should_log_progress(done: int, total: int, last_percent: int) -> bool:
    if total <= 0:
        return False
    percent = int(done * 100 / total)
    if done == 1 or done == total:
        return True
    if percent >= last_percent + 10:
        return True
    return False
 
class KGFileManager:
    """Thread-safe JSONL file writer with automatic size-based rotation.

    Manages writing knowledge graph records (nodes or relations) to JSONL files,
    automatically rotating to a new file when the current one exceeds a size limit.

    File naming: ``{file_type}s_{index:04d}.jsonl``
    (e.g. ``relations_0000.jsonl``, ``nodes_0001.jsonl``)

    Args:
        base_path: Base directory for output files.
        file_type: Record type, ``"relation"`` or ``"node"``.
        max_size_bytes: Maximum bytes per file before rotation (default 3 GB).
        start_index: Starting file index number.
    """

    def __init__(
        self,
        base_path: Path | str,
        file_type: str,
        max_size_bytes: int = 3 * 1024 * 1024 * 1024,
        start_index: int = 0,
    ):
        self.base_path = Path(base_path)
        self.file_type = file_type
        self.max_size_bytes = max_size_bytes
        self.current_file_index = start_index
        self.current_file_size = 0
        self.current_file: TextIO | None = None
        self.lock = threading.Lock()
        self.records_written = 0
        self._open_next_file()

    def _get_file_path(self) -> Path:
        """Return the path for the current file index."""
        filename = f"{self.file_type}s_{self.current_file_index:04d}.jsonl"
        return self.base_path / filename

    def _open_next_file(self):
        """Close the current file (if any) and open the next one."""
        if self.current_file:
            self.current_file.close()
            logger.info(
                "Closed %s file: %d records (%.2f MB)",
                self.file_type,
                self.records_written,
                self.current_file_size / 1024 / 1024,
            )

        file_path = self._get_file_path()
        self.current_file = open(file_path, "w", encoding="utf-8")
        self.current_file_size = 0
        self.records_written = 0
        logger.info("Writing %s file: %s", self.file_type, file_path)

    def write_record(self, record: dict):
        """Write a single JSON record, rotating the file if size limit is exceeded."""
        with self.lock:
            json_str = json.dumps(record, ensure_ascii=False) + "\n"
            str_size = len(json_str.encode("utf-8"))

            if self.current_file_size + str_size > self.max_size_bytes:
                self.current_file_index += 1
                self._open_next_file()

            if self.current_file is None:
                raise RuntimeError("File not properly opened")
            self.current_file.write(json_str)
            self.current_file.flush()
            self.current_file_size += str_size
            self.records_written += 1

            if self.records_written % 1000 == 0:
                logger.debug(
                    "%s file: %d records written (%.2f MB)",
                    self.file_type,
                    self.records_written,
                    self.current_file_size / 1024 / 1024,
                )

    def close(self):
        """Close the current file."""
        if self.current_file:
            self.current_file.close()
            logger.info(
                "Final close %s file: %d records (%.2f MB)",
                self.file_type,
                self.records_written,
                self.current_file_size / 1024 / 1024,
            )


def save_kg_data(
    relations: list[dict],
    nodes: list[dict],
    output_dir: Path | str,
    batch_size: int = 2000,
    file_manager_rel: KGFileManager | None = None,
    file_manager_node: KGFileManager | None = None,
) -> None:
    """Save extracted KG nodes and relations to JSONL files.

    When *file_manager_rel* / *file_manager_node* are provided, records are
    written through them (with automatic size-based rotation).  Otherwise
    falls back to simple append-mode writing into a single file per type.
    """
    output_dir = Path(output_dir)

    if file_manager_rel is not None:
        for rel in relations:
            file_manager_rel.write_record(rel)
    else:
        relation_dir = output_dir / "relation"
        relation_dir.mkdir(parents=True, exist_ok=True)
        relations_file = relation_dir / "relations.jsonl"
        with open(relations_file, "a", encoding="utf-8") as f_rel:
            for rel in relations:
                f_rel.write(json.dumps(rel, ensure_ascii=False) + "\n")
        logger.info("Saved %d relations to %s", len(relations), relations_file)

    if file_manager_node is not None:
        for node in nodes:
            file_manager_node.write_record(node)
    else:
        node_dir = output_dir / "node"
        node_dir.mkdir(parents=True, exist_ok=True)
        nodes_file = node_dir / "nodes.jsonl"
        with open(nodes_file, "a", encoding="utf-8") as f_node:
            for node in nodes:
                f_node.write(json.dumps(node, ensure_ascii=False) + "\n")
        logger.info("Saved %d nodes to %s", len(nodes), nodes_file)


def batch_extract_kg_from_chunks(
    text_chunks: list[tuple[str, str]],
    entity_schema_str: str,
    relation_schema_str: str,
    use_llm: Callable[[str], str],
    prompts: dict[str, str],
    parse_nodes: Callable[[str], list[dict]] = parse_nodes,
    get_sha256_hash: Callable[[str], str] = get_sha256_hash,
    output_dir: Path | str | None = None,
    batch_size: int = 2000,
    max_workers: int = 4,
    show_progress: bool = True,
    max_file_size_bytes: int = 3 * 1024 * 1024 * 1024,
) -> tuple[list[dict], list[dict]]:
    """Extract entities and relations from text chunks using multi-threaded LLM calls.

    Each chunk is processed by ``extract_kg_from_chunk`` in a thread pool.
    Results are periodically flushed to JSONL files (when *output_dir* is set)
    once the in-memory buffer reaches *batch_size*.

    Args:
        text_chunks: List of ``(chunk_id, text_content)`` pairs.
        entity_schema_str: Entity schema definition (double-brace format).
        relation_schema_str: Relation schema definition.
        use_llm: LLM call function.
        prompts: Prompt template dict containing ``node_prompt``, ``rel_prompt``,
            ``entity_template``, and ``relation_template``.
        parse_nodes: Parser for LLM output into structured dicts.
        get_sha256_hash: Hash function for generating record IDs.
        output_dir: If set, results are saved to this directory.
        batch_size: Number of records buffered before flushing to disk.
        max_workers: Maximum number of concurrent threads.
        show_progress: Whether to display a tqdm progress bar.
        max_file_size_bytes: Maximum bytes per output file.

    Returns:
        ``(all_relations, all_nodes)`` — accumulated results. When *output_dir*
        is specified, most data has already been flushed to disk and the returned
        lists contain only the final unflushed remainder.
    """
    all_relations = []
    all_nodes = []

    # Fall back to default templates when schema strings are empty
    entity_template = prompts["entity_template"]
    relation_template = prompts["relation_template"]
    if entity_schema_str == "" or relation_schema_str == "":
        entity_schema_str = entity_template
        relation_schema_str = relation_template

    file_manager_rel = None
    file_manager_node = None
    if output_dir:
        output_dir = Path(output_dir)
        relation_dir = output_dir / "relation"
        node_dir = output_dir / "node"
        relation_dir.mkdir(parents=True, exist_ok=True)
        node_dir.mkdir(parents=True, exist_ok=True)

        file_manager_rel = KGFileManager(
            base_path=relation_dir,
            file_type="relation",
            max_size_bytes=max_file_size_bytes,
            start_index=0,
        )
        file_manager_node = KGFileManager(
            base_path=node_dir,
            file_type="node",
            max_size_bytes=max_file_size_bytes,
            start_index=0,
        )
        logger.info(
            "Initialized file managers: max %.2f GB per file",
            max_file_size_bytes / 1024 / 1024 / 1024,
        )

    def process_chunk(args):
        chunk_id, text = args
        try:
            relations, nodes = extract_kg_from_chunk(
                input_text=text,
                chunk_id=chunk_id,
                entity_schema_str=entity_schema_str,
                relation_schema_str=relation_schema_str,
                use_llm=use_llm,
                parse_nodes=parse_nodes,
                get_sha256_hash=get_sha256_hash,
                prompts=prompts,
            )
            return chunk_id, relations, nodes, None
        except Exception as e:
            logger.error("Failed to process chunk %s: %s", chunk_id, e)
            return chunk_id, [], [], e

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        FutureResult = Future[
            tuple[
                str,
                list[dict[str, Any]],
                list[dict[str, Any]],
                Exception | None,
            ]
        ]

        futures_dict: dict[FutureResult, tuple[str, str]] = {  # type: ignore
            executor.submit(process_chunk, chunk): chunk for chunk in text_chunks
        }
        progress_iter = as_completed(futures_dict)

        total_chunks = len(text_chunks)
        completed = 0
        last_logged_percent = -1
        if show_progress and total_chunks:
            logger.info("Extracting KG: 0/%d (0%%)", total_chunks)

        for future in progress_iter:
            chunk_id, relations, nodes, error = future.result()
            completed += 1

            if error is None:
                all_relations.extend(relations)
                all_nodes.extend(nodes)

                if output_dir and (
                    len(all_relations) >= batch_size or len(all_nodes) >= batch_size
                ):
                    save_batch_relations = all_relations[:batch_size]
                    save_batch_nodes = all_nodes[:batch_size]

                    save_kg_data(
                        save_batch_relations,
                        save_batch_nodes,
                        output_dir,
                        batch_size,
                        file_manager_rel=file_manager_rel,
                        file_manager_node=file_manager_node,
                    )

                    all_relations = all_relations[batch_size:]
                    all_nodes = all_nodes[batch_size:]

                logger.debug(
                    "Processed %s: %d relations, %d nodes",
                    chunk_id, len(relations), len(nodes),
                )
            else:
                logger.warning("Failed chunk %s: %s", chunk_id, error)

            if show_progress and total_chunks:
                percent = int(completed * 100 / total_chunks)
                if _should_log_progress(completed, total_chunks, last_logged_percent):
                    logger.info(
                        "Extracting KG: %d/%d (%d%%)",
                        completed,
                        total_chunks,
                        percent,
                    )
                    last_logged_percent = percent

    # Flush remaining data
    if output_dir and (all_relations or all_nodes):
        save_kg_data(
            all_relations,
            all_nodes,
            output_dir,
            batch_size,
            file_manager_rel=file_manager_rel,
            file_manager_node=file_manager_node,
        )

    if file_manager_rel:
        file_manager_rel.close()
    if file_manager_node:
        file_manager_node.close()

    logger.info(
        "Batch extraction done: %d chunks processed, %s relations, %s nodes",
        len(text_chunks),
        len(all_relations) if not output_dir else "all",
        len(all_nodes) if not output_dir else "all",
    )

    return all_relations, all_nodes


def extract_kg_from_chunk(
    input_text: str,
    chunk_id: str,
    entity_schema_str: str,
    relation_schema_str: str,
    use_llm: Callable[[str], str],
    prompts: dict[str, str],
    parse_nodes: Callable[[str], list[dict]] = parse_nodes,
    get_sha256_hash: Callable[[str], str] = get_sha256_hash,
) -> tuple[list[dict], list[dict]]:
    """Extract entities and relations from a single text chunk via LLM.

    Pipeline: entity extraction → relation extraction → metadata enrichment.
    Each node/relation dict is augmented with ``chunk_id`` and a SHA-256
    based ``Id`` derived from its ``Description`` field.

    Args:
        input_text: Raw text content to process.
        chunk_id: Unique identifier of the source chunk.
        entity_schema_str: Entity schema definition (double-brace format).
        relation_schema_str: Relation schema definition.
        use_llm: LLM call function.
        prompts: Prompt template dict with keys ``node_prompt`` and ``rel_prompt``.
        parse_nodes: Parser that converts LLM output to a list of dicts.
        get_sha256_hash: Hash function for generating record IDs.

    Returns:
        ``(relations, nodes)`` — lists of extracted relation and node dicts.
    """
    try:
        node_prompt = prompts["node_prompt"].format(
            entity_schema=entity_schema_str, input_text=input_text
        )
        node_res = use_llm(node_prompt)
        nodes = parse_nodes(node_res)

        if not nodes:
            logger.debug("Chunk %s: no nodes extracted", chunk_id)
            return [], []

        rel_prompt = prompts["rel_prompt"].format(
            relation_schema=relation_schema_str, input_text=input_text, nodes=nodes
        )
        rel_res = use_llm(rel_prompt)
        rel = parse_nodes(rel_res)

        for r in rel:
            r["chunk_id"] = chunk_id
            description = r.get("Description", str(r))
            r["Id"] = get_sha256_hash(description)

        for n in nodes:
            n["chunk_id"] = chunk_id
            description = n.get("Description", str(n))
            n["Id"] = get_sha256_hash(description)

        logger.debug(
            "Chunk %s: extracted %d relations, %d nodes",
            chunk_id, len(rel), len(nodes),
        )

        return rel, nodes

    except KeyError as e:
        logger.error("Chunk %s: missing required prompt key %s", chunk_id, e)
        return [], []
    except Exception as e:
        logger.warning("Chunk %s: KG extraction failed: %s", chunk_id, e)
        return [], []


def extract_relation_from_csv(csv_path: Path | str) -> str:
    """Read a relation CSV and return a text schema with one entry per relation.

    Expected CSV columns (Chinese headers): ``关系类型``, ``关系``, ``关系说明``.
    Entries are joined by ``---`` separators.
    """
    csv_path = Path(csv_path)
    schema_lines = []

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            relation_type = row.get("关系类型", "").strip()
            relation = row.get("关系", "").strip()
            description = row.get("关系说明", "").strip()

            schema_line = (
                f"关系类型: {relation_type}\n关系: {relation}\n关系说明: {description}"
            )
            schema_lines.append(schema_line)

    return "\n---\n".join(schema_lines)


def extract_entity_from_csv(csv_path: Path | str, indent: int = 2) -> str:
    """Read an entity CSV and convert it to a double-brace JSON schema string.

    The CSV is expected to have four columns (level-1 node, level-2 node,
    level-3 node, attribute).  Empty cells inherit the value from the
    previous row.  The result uses ``{{ }}`` instead of ``{ }`` so it can
    be safely embedded in Python ``.format()`` templates.
    """
    csv_path = Path(csv_path)
    nested_dict: defaultdict[str, defaultdict[str, defaultdict[str, list[str]]]] = (
        defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )

    current_level1 = ""
    current_level2 = ""
    current_level3 = ""

    EXPECTED_CSV_COLUMNS = 4

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header

        for row in reader:
            if len(row) < EXPECTED_CSV_COLUMNS:
                continue

            level1 = row[0].strip() if row[0].strip() else current_level1
            level2 = row[1].strip() if row[1].strip() else current_level2
            level3 = row[2].strip() if row[2].strip() else current_level3
            attr = row[3].strip()

            if row[0].strip():
                current_level1 = level1
            if row[1].strip():
                current_level2 = level2
            if row[2].strip():
                current_level3 = level3

            if attr:
                nested_dict[level1][level2][level3].append(attr)

    nested_dict_final: dict[str, dict[str, dict[str, list[str]]]] = {
        k1: {k2: {k3: v3 for k3, v3 in v2.items()} for k2, v2 in v1.items()}
        for k1, v1 in nested_dict.items()
    }

    json_str = json.dumps(nested_dict_final, ensure_ascii=False, indent=indent)
    prompt_str = json_str.replace("{", "{{").replace("}", "}}")

    return prompt_str


def load_chunks_from_jsonl(input_dir: Path | str) -> list[tuple[str, str]]:
    """Load all ``.jsonl`` files from *input_dir* and return ``[(chunk_id, text)]``."""
    input_dir = Path(input_dir)
    if not input_dir.exists():
        logger.warning("Directory does not exist: %s", input_dir)
        return []

    jsonl_files = list(input_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.warning("No .jsonl files found in %s", input_dir)
        return []

    results: list[tuple[str, str]] = []
    for jsonl_file in sorted(jsonl_files):
        file_count = 0
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    obj = json.loads(line_stripped)
                    chunk_id = str(obj.get("source_chunk", "")).strip()
                    text = str(obj.get("text", ""))
                    if chunk_id and text:
                        results.append((chunk_id, text))
                        file_count += 1
                except Exception as e:
                    logger.warning("Failed to parse chunk line (%s): %s", jsonl_file, e)
                    continue
        logger.info("Loaded %d chunks from %s", file_count, jsonl_file)

    logger.info("Total: %d chunks from %d files", len(results), len(jsonl_files))
    return results
