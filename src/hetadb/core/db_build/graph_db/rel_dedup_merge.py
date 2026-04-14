"""Relation deduplication and merging for knowledge graph construction.

Provides LLM-based relation dedup, embedding generation,
batch clustering + merge pipeline, and Milvus-backed global dedup.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pymilvus import Collection

from hetadb.core.db_build.graph_db.graph_vector import (
    FileManager, check_embedding_connection, process_file, embedding,
)
from hetadb.utils.hash_filename import get_sha256_hash
from hetadb.utils.utils import (
    normalize_name,
    _parse_llm_response,
    cluster_by_embedding,
    iter_embedding_records,
    take_n,
    write_jsonl,
)
from hetadb.core.db_build.sql_db.sql_db import (
    create_graph_tables,
    insert_relations_to_pg,
    delete_relations_from_pg,
    insert_cluster_chunk_relations,
    delete_cluster_chunk_relations_by_cluster_ids,
    get_chunk_source_mapping,
)
from hetadb.core.db_build.vector_db.vector_db import (
    ensure_rel_collection,
    connect_milvus,
    rel_milvus_to_record_format,
    insert_relations_to_milvus,
    delete_relations_from_milvus,
    search_similar_relations,
)

logger = logging.getLogger(__name__)

# Type alias for relation grouping key: (Node1, Node2, Relation, Type)
RelationKey = tuple[str, str, str, str]

# Separator used in mapping_table JSON keys to encode node pairs as strings.
_NODE_PAIR_SEP = "||"


def _parse_node_pair_key(key: Any) -> tuple[str, str] | None:
    """Parse a mapping_table key into a (Node1, Node2) tuple.

    Supports multiple formats the LLM might produce:
    - ``"A||B"`` (preferred ``||``-separated string)
    - ``["A", "B"]`` (JSON array, impossible as a JSON key but may occur after
      custom post-processing)
    - ``'("A", "B")'`` / ``"('A', 'B')"`` (Python tuple string)
    """
    if isinstance(key, (list, tuple)) and len(key) == 2:
        return (normalize_name(key[0]), normalize_name(key[1]))

    if not isinstance(key, str):
        return None

    # "A||B"
    if _NODE_PAIR_SEP in key:
        parts = key.split(_NODE_PAIR_SEP, maxsplit=1)
        if len(parts) == 2:
            a, b = normalize_name(parts[0]), normalize_name(parts[1])
            if a and b:
                return (a, b)

    # Try JSON array string: '["A", "B"]'
    try:
        parsed = json.loads(key)
        if isinstance(parsed, list) and len(parsed) == 2:
            a, b = normalize_name(parsed[0]), normalize_name(parsed[1])
            if a and b:
                return (a, b)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try Python tuple string: '("A", "B")' or "('A', 'B')"
    stripped = key.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        inner = stripped[1:-1]
        parts = [p.strip().strip("'\"") for p in inner.split(",", maxsplit=1)]
        if len(parts) == 2:
            a, b = normalize_name(parts[0]), normalize_name(parts[1])
            if a and b:
                return (a, b)

    return None


def _parse_node_pair_value(item: Any) -> tuple[str, str] | None:
    """Parse a single mapping_table value element into a (Node1, Node2) tuple.

    Supports JSON arrays ``["A", "B"]`` and the same string formats as
    :func:`_parse_node_pair_key`.
    """
    if isinstance(item, (list, tuple)) and len(item) == 2:
        a, b = normalize_name(item[0]), normalize_name(item[1])
        if a and b:
            return (a, b)
    if isinstance(item, str):
        return _parse_node_pair_key(item)
    return None


def dedup_relations(
    use_llm,
    rel_dedup_prompt: str,
    input_path: Path,
    mapping_path: Path,
    output_path: Path,
    workers: int,
    max_rounds: int,
    llm_batch_size: int,
) -> None:
    """Multi-round LLM-based relation deduplication.

    Reads relation JSONL, applies node-name mapping, groups by
    (Node1, Node2, Relation, Type), merges duplicates via LLM,
    and iterates until no duplicates remain or *max_rounds* is reached.

    Args:
        use_llm: LLM callable for merge decisions.
        rel_dedup_prompt: Prompt template for relation dedup.
        input_path: Input relation JSONL file.
        mapping_path: Node-name mapping JSON (old -> canonical).
        output_path: Output deduplicated relation JSONL file.
        workers: Number of parallel threads.
        max_rounds: Maximum number of dedup iterations (default 10).
    """

    def load_mapping(path: Path) -> dict[str, str]:
        """Load node-name mapping file."""
        if not path.exists():
            logger.warning("Mapping file not found: %s", path)
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {normalize_name(k): normalize_name(v) for k, v in data.items()}
        except Exception as exc:
            logger.error("Failed to load mapping file: %s", exc)
            return {}

    def map_node(name: str, mapping: dict[str, str]) -> str:
        """Map a node name to its canonical form."""
        norm = normalize_name(name)
        return mapping.get(norm, norm)

    def _relation_key(record: dict[str, Any]) -> RelationKey:
        """Generate a grouping key for a relation record."""
        return (
            normalize_name(record.get("Node1", "")),
            normalize_name(record.get("Node2", "")),
            normalize_name(record.get("Relation", "")),
            normalize_name(record.get("Type", "")),
        )

    def read_relations(
        input_path: Path, mapping: dict[str, str],
    ) -> tuple[dict[RelationKey, dict[str, Any]], dict[RelationKey, list[dict[str, Any]]]]:
        """Read relation JSONL and group duplicates by relation key."""
        uniques: dict[RelationKey, dict[str, Any]] = {}
        duplicates: dict[RelationKey, list[dict[str, Any]]] = defaultdict(list)

        with input_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping invalid JSON at line %s: %s", line_no, exc)
                    continue

                n1 = map_node(record.get("Node1", ""), mapping)
                n2 = map_node(record.get("Node2", ""), mapping)
                if not n1 or not n2:
                    logger.warning("Line %s missing Node1/Node2, skipped", line_no)
                    continue

                record["Node1"] = n1
                record["Node2"] = n2

                key = _relation_key(record)
                if key in uniques:
                    if key not in duplicates:
                        duplicates[key].append(uniques[key])
                    duplicates[key].append(record)
                else:
                    uniques[key] = record

        logger.info("Scan complete: %d unique relations, %d duplicate groups", len(uniques), len(duplicates))
        return uniques, duplicates

    def split_uniques_duplicates_from_records(
        records,
    ) -> tuple[dict[RelationKey, dict[str, Any]], dict[RelationKey, list[dict[str, Any]]]]:
        """Re-partition in-memory records into unique and duplicate groups."""
        uniques: dict[RelationKey, dict[str, Any]] = {}
        duplicates: dict[RelationKey, list[dict[str, Any]]] = defaultdict(list)

        for rec in records:
            key = _relation_key(rec)
            if not key[0] or not key[1]:
                continue
            if key in uniques:
                if key not in duplicates:
                    duplicates[key].append(uniques[key])
                duplicates[key].append(rec)
            else:
                uniques[key] = rec

        return uniques, duplicates

    def build_prompt(node1: str, node2: str, relations: list[dict[str, Any]]) -> str:
        """Build an LLM prompt from a node pair and their relations."""
        relation_block = json.dumps(relations, ensure_ascii=False, indent=2)
        return rel_dedup_prompt.format(
            node1=node1, node2=node2, relation_block=relation_block,
        )

    def merge_with_llm(
        node1: str, node2: str, relations: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Iterative LLM-based merge: process relations in batches until fully merged."""
        split_relations: list[dict[str, Any]] = []

        def _select_main_and_split(parsed_resp: Any) -> dict[str, Any]:
            if isinstance(parsed_resp, dict):
                return parsed_resp
            if isinstance(parsed_resp, list):
                main_relation: dict[str, Any] | None = None
                for item in parsed_resp:
                    if not isinstance(item, dict):
                        continue
                    n1 = normalize_name(item.get("Node1", ""))
                    n2 = normalize_name(item.get("Node2", ""))
                    if main_relation is None and n1 == normalize_name(node1) and n2 == normalize_name(node2):
                        main_relation = item
                    else:
                        split_relations.append(item)
                if main_relation is None:
                    for item in parsed_resp:
                        if isinstance(item, dict):
                            main_relation = item
                            break
                return main_relation or {}
            logger.error("Unhandled LLM response type: %s", type(parsed_resp))
            return {}

        if len(relations) <= llm_batch_size:
            resp_str = use_llm(prompt=build_prompt(node1, node2, relations))
            parsed = _parse_llm_response(resp_str, logger)
            main_relation = _select_main_and_split(parsed)
            return main_relation, split_relations

        accumulated_result = None
        total_batches = (len(relations) + llm_batch_size - 1) // llm_batch_size
        for batch_idx in range(total_batches):
            start_idx = batch_idx * llm_batch_size
            end_idx = min(start_idx + llm_batch_size, len(relations))
            batch_relations = relations[start_idx:end_idx]

            logger.info(
                "Processing %s -> %s batch %d/%d (%d relations)",
                node1, node2, batch_idx + 1, total_batches, len(batch_relations),
            )

            if accumulated_result is not None:
                relations_to_merge = [accumulated_result] + batch_relations
            else:
                relations_to_merge = batch_relations

            resp_str = use_llm(prompt=build_prompt(node1, node2, relations_to_merge))
            parsed = _parse_llm_response(resp_str, logger)
            accumulated_result = _select_main_and_split(parsed)
            if not accumulated_result:
                logger.error(
                    "Parse failed for %s -> %s batch %d/%d, empty result",
                    node1, node2, batch_idx + 1, total_batches,
                )

        return accumulated_result if accumulated_result is not None else {}, split_relations

    # -- Main dedup logic --
    mapping = load_mapping(mapping_path)
    uniques, duplicates = read_relations(input_path, mapping)

    if not duplicates:
        logger.info("No duplicate relations found, copying input to output")
        write_jsonl(uniques.values(), output_path)
        return

    current_uniques, current_duplicates = uniques, duplicates
    output_records: list[dict[str, Any]] = list(current_uniques.values())

    total_rounds = 0
    total_groups = 0
    total_main = 0
    total_split = 0

    def _merge_task(
        node1: str, node2: str, records: list[dict[str, Any]],
    ) -> tuple[RelationKey, dict[str, Any], list[dict[str, Any]]]:
        """Dedup-merge a single group of same-key relations."""
        merged, split_relations = merge_with_llm(node1, node2, records)

        chunk_ids: set = set()

        def _add_chunk(val: Any) -> None:
            if not val:
                return
            if isinstance(val, list):
                for v in val:
                    if v:
                        chunk_ids.add(str(v))
            else:
                chunk_ids.add(str(val))

        for rec in records:
            _add_chunk(rec.get("chunk_id"))
        _add_chunk(merged.get("chunk_id"))

        if chunk_ids:
            merged["chunk_id"] = sorted(chunk_ids)

        desc_text = str(merged.get("Description", ""))
        rel_text = normalize_name(merged.get("Relation", ""))
        type_text = normalize_name(merged.get("Type", ""))
        merged["Id"] = get_sha256_hash(f"{node1}::{rel_text}::{node2}::{desc_text}")

        for split in split_relations:
            desc_split = str(split.get("Description", ""))
            rel_split = normalize_name(split.get("Relation", ""))
            split["Id"] = get_sha256_hash(
                f"{split.get('Node1', '')}::{rel_split}::{split.get('Node2', '')}::{desc_split}"
            )

        key = (
            normalize_name(merged.get("Node1", node1)),
            normalize_name(merged.get("Node2", node2)),
            rel_text,
            type_text,
        )
        return key, merged, split_relations

    while current_duplicates and total_rounds < max_rounds:
        total_rounds += 1
        logger.info("Starting dedup round %d, %d duplicate groups", total_rounds, len(current_duplicates))

        # Maps orig_key -> (merged_record, filtered_split_relations)
        # Using orig_key (not merged_key) so output rebuild can correctly replace the original.
        merge_map: dict[RelationKey, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        round_stats: dict[RelationKey, dict[str, int]] = {}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_key = {
                executor.submit(_merge_task, k[0], k[1], records): k
                for k, records in current_duplicates.items()
            }
            total = len(future_to_key)
            for idx, future in enumerate(as_completed(future_to_key), 1):
                orig_key = future_to_key[future]
                try:
                    merged_key, merged, split_relations = future.result()

                    merge_map[orig_key] = (merged, split_relations)
                    round_stats[orig_key] = {
                        "merged": 1 if merged else 0,
                        "split": len(split_relations),
                    }
                    logger.info(
                        "(%d/%d) Merged relation: %s -> %s (%d splits)",
                        idx, total, orig_key[0], orig_key[1], len(split_relations),
                    )
                except Exception as exc:
                    logger.error("Failed to merge relation %s -> %s: %s", orig_key[0], orig_key[1], exc)

        # Rebuild output: replace each original duplicate representative with its
        # merged result (keyed by orig_key, not merged_key, to avoid lookup mismatch).
        #
        # Pre-compute the set of keys already committed to output so that splits
        # whose key collides with any existing record (or with a sibling split)
        # are discarded rather than creating new duplicate groups next round.
        output_key_set: set[RelationKey] = set()
        for key, record in current_uniques.items():
            if key in merge_map:
                merged_record, _ = merge_map[key]
                output_key_set.add(_relation_key(merged_record))
            else:
                output_key_set.add(key)

        output_records = []
        extra_relations: list[dict[str, Any]] = []
        for key, record in current_uniques.items():
            if key in merge_map:
                merged_record, splits = merge_map[key]
                output_records.append(merged_record)
                for split in splits:
                    sk = _relation_key(split)
                    if sk not in output_key_set:
                        extra_relations.append(split)
                        output_key_set.add(sk)
                    else:
                        logger.info(
                            "Filtered split with occupied key (%s -> %s) for group %s -> %s",
                            sk[0], sk[1], key[0], key[1],
                        )
            else:
                output_records.append(record)
        output_records.extend(extra_relations)

        if round_stats:
            round_split = sum(v["split"] for v in round_stats.values())
            round_main = sum(v["merged"] for v in round_stats.values())
            logger.info(
                "Round %d stats: %d groups, %d main relations, %d splits",
                total_rounds, len(round_stats), round_main, round_split,
            )
            total_groups += len(round_stats)
            total_main += round_main
            total_split += round_split

        current_uniques, current_duplicates = split_uniques_duplicates_from_records(output_records)
        if current_duplicates:
            logger.info(
                "Round %d: %d duplicate groups remain, continuing",
                total_rounds, len(current_duplicates),
            )

    if current_duplicates:
        logger.warning(
            "Reached max rounds (%d) with %d duplicate groups still remaining",
            max_rounds, len(current_duplicates),
        )

    write_jsonl(output_records, output_path)
    if total_rounds > 0:
        logger.info(
            "Dedup summary: %d rounds, %d groups, %d main relations, %d splits",
            total_rounds, total_groups, total_main, total_split,
        )
    logger.info("Dedup complete, output: %s, %d records", output_path, len(output_records))


def embed_rels(
    api_key: str,
    embedding_url: str,
    embedding_model: str,
    embedding_timeout: int,
    rels_input_path: str,
    output_dir: str,
    batch_size: int,
    max_file_size_bytes: int,
    num_threads: int,
    max_retries: int,
    retry_delay: int,
    embedding_dim: int,
) -> int:
    """Generate embeddings for deduplicated relations and write to JSONL files."""
    logger.info("Starting relation embedding process")
    ok = check_embedding_connection(
        api_key, embedding_url, embedding_model, embedding_timeout,
    )
    if not ok:
        logger.error("Embedding API test failed")
        return 0

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    file_manager_attr = FileManager(
        output_dir, "rels", max_size_bytes=max_file_size_bytes, start_index=0,
    )
    rel_count = process_file(
        rels_input_path,
        "rels",
        file_manager_attr,
        batch_size,
        num_threads,
        api_key,
        embedding_url,
        embedding_model,
        embedding_timeout,
        max_retries,
        retry_delay,
        embedding_dim,
    )
    file_manager_attr.close()
    logger.info("Processed %d relation records", rel_count)
    return rel_count


def run_rel_merge_pipeline(
    embedding_dir: str,
    output_dir: str,
    use_llm,
    emb_cfg,
    merge_rel_prompt,
    batch_size: int,
    n: int,
    sim_threshold: float,
    temperature: float,
    max_workers: int,
    llm_max_retries: int,
) -> None:
    """Batch clustering + LLM merge pipeline for deduplicated relation embeddings.

    Reads embedding records in batches of ``n * batch_size``, clusters them
    by cosine similarity, merges similar relations via LLM, and writes per-round
    output files plus a global mapping table.

    Args:
        embedding_dir: Directory containing ``*.jsonl`` embedding files.
        output_dir: Output directory for merged results and mappings.
        use_llm: LLM callable.
        emb_cfg: Embedding API config dict.
        merge_rel_prompt: Prompt template for relation merging.
        batch_size: Records per batch.
        n: Number of batches per round / intra-batch concurrency.
        sim_threshold: Cosine similarity threshold for clustering.
        temperature: LLM temperature.
    """

    def build_relations_for_prompt(cluster) -> list[dict[str, Any]]:
        """Trim relation records to fields needed by the LLM prompt."""
        return [
            {
                "Node1": rec.get("Node1", ""),
                "Node2": rec.get("Node2", ""),
                "Relation": rec.get("Relation", ""),
                "Type": rec.get("Type", ""),
                "Description": rec.get("Description", ""),
            }
            for rec in cluster
        ]

    def llm_merge_rel_cluster(
        cluster,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Call LLM to merge/split a single cluster of similar relations."""
        if not cluster:
            return [], {}

        rels_for_prompt = build_relations_for_prompt(cluster)
        relations_json = json.dumps(rels_for_prompt, ensure_ascii=False, indent=2)
        prompt = merge_rel_prompt.format(relations_json=relations_json)

        parsed = None
        for _ in range(llm_max_retries):
            resp = use_llm(
                prompt=prompt,
                response_format={"type": "json_object"},
                temperature=temperature,
            )
            if not resp or resp == "Error":
                continue
            parsed = _parse_llm_response(resp, logger)
            if parsed:
                break

        if not parsed:
            logger.info("LLM parse failed, returning original cluster (%d records)", len(cluster))
            return list(cluster), {}

        rel_list = parsed.get("relation_list", [])
        mapping_table = parsed.get("mapping_table", {}) or {}
        if not isinstance(rel_list, list):
            logger.info("Invalid relation_list, returning original cluster (%d records)", len(cluster))
            return list(cluster), {}

        # Normalize mapping_table: {(n1,n2): [(o1,o2), ...]}
        # JSON keys are always strings, so we parse "Node1||Node2" format
        normalized_mapping: dict[tuple[str, str], list[tuple[str, str]]] = {}
        if isinstance(mapping_table, dict):
            for k, v in mapping_table.items():
                canon = _parse_node_pair_key(k)
                if canon is None or not isinstance(v, list):
                    continue
                originals = []
                for item in v:
                    pair = _parse_node_pair_value(item)
                    if pair is not None:
                        originals.append(pair)
                if originals:
                    normalized_mapping[canon] = originals

        if not normalized_mapping:
            logger.info("Empty mapping_table after parsing, returning original cluster (%d records)", len(cluster))
            return list(cluster), {}

        # Build Node1+Node2 index for looking up original records
        index_n1n2: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for rec in cluster:
            key = (normalize_name(rec.get("Node1", "")), normalize_name(rec.get("Node2", "")))
            index_n1n2.setdefault(key, []).append(rec)

        # Build index for merge_tag=true entries from LLM result
        merged_rel_info: dict[tuple[str, str], dict[str, Any]] = {}
        for rel in rel_list:
            if isinstance(rel, dict) and rel.get("merge_tag"):
                key = (normalize_name(rel.get("Node1", "")), normalize_name(rel.get("Node2", "")))
                merged_rel_info[key] = rel

        merged_output: list[dict[str, Any]] = []
        mapping: dict[str, list[str]] = {}
        used_pairs: set = set()

        for canon_pair, original_pairs in normalized_mapping.items():
            canon_n1, canon_n2 = canon_pair
            rel_tpl = merged_rel_info.get(canon_pair)
            if not rel_tpl:
                continue

            relation = rel_tpl.get("Relation", "")
            rtype = rel_tpl.get("Type", "")
            desc = rel_tpl.get("Description", "")
            attr = rel_tpl.get("Attr", {}) or {}
            if not desc:
                continue

            # Collect related original records from mapping
            related_recs: list[dict[str, Any]] = []
            for op in original_pairs:
                recs = index_n1n2.get(op, [])
                related_recs.extend(recs)
            if not related_recs:
                continue

            # Merge chunk_ids
            chunk_ids: list[str] = []
            seen: set = set()
            for r in related_recs:
                cid = r.get("chunk_id")
                if isinstance(cid, list):
                    for v in cid:
                        if v and v not in seen:
                            seen.add(v)
                            chunk_ids.append(v)
                elif cid:
                    if cid not in seen:
                        seen.add(cid)
                        chunk_ids.append(cid)

            new_id = get_sha256_hash(desc or f"{canon_n1}-{relation}-{canon_n2}")
            new_embedding = embedding(desc or f"{canon_n1}-{relation}-{canon_n2}", **emb_cfg)[0]

            merged_rec = {
                "Node1": canon_n1,
                "Node2": canon_n2,
                "Relation": relation,
                "Type": rtype,
                "Description": desc,
                "Attr": attr,
                "chunk_id": chunk_ids,
                "Id": new_id,
                "embedding": new_embedding,
                "merge_tag": True,
            }
            merged_output.append(merged_rec)
            logger.info(
                "Cluster merge: (%s, %s) relation=%s, %d original records, %d chunk_ids",
                canon_n1, canon_n2, relation, len(related_recs), len(chunk_ids),
            )
            used_pairs.update(original_pairs)

        # Pass through relations not in the mapping table
        untouched = 0
        for key, recs in index_n1n2.items():
            if key in used_pairs:
                continue
            for rec in recs:
                out = dict(rec)
                out["merge_tag"] = False
                out["source_ids"] = [rec.get("Id", "")]
                merged_output.append(out)
                untouched += 1
        logger.info(
            "Cluster done: %d merged, %d untouched, %d original",
            len([m for m in merged_output if m.get("merge_tag")]),
            untouched, len(cluster),
        )

        if not merged_output:
            return list(cluster), {}

        return merged_output, mapping

    def merge_records_with_llm(
        records, similarity_threshold: float, temperature: float,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Cluster records by embedding similarity, then LLM-merge each multi-item cluster."""
        clusters = cluster_by_embedding(records, similarity_threshold)
        merged: list[dict[str, Any]] = []
        all_mappings: dict[str, list[str]] = {}

        llm_clusters = []
        for cluster in clusters:
            if len(cluster) == 1:
                rec = dict(cluster[0])
                rec["merge_tag"] = False
                rec["source_ids"] = [rec.get("Id", "")]
                merged.append(rec)
            else:
                llm_clusters.append(cluster)

        if llm_clusters:
            pool_size = min(len(llm_clusters), max(1, max_workers // n))
            with ThreadPoolExecutor(max_workers=pool_size) as ex:
                futures = [ex.submit(llm_merge_rel_cluster, cluster, temperature) for cluster in llm_clusters]
                for fut in as_completed(futures):
                    cluster_merged, cluster_mapping = fut.result()
                    merged.extend(cluster_merged)
                    for k, v in cluster_mapping.items():
                        if k in all_mappings:
                            exist = set(all_mappings[k])
                            for i in v:
                                if i not in exist:
                                    all_mappings[k].append(i)
                                    exist.add(i)
                        else:
                            all_mappings[k] = list(v)

        return merged, all_mappings

    def merge_mapping_tables(*tables: dict[str, list[str]]) -> dict[str, list[str]]:
        """Merge multiple mapping tables, deduplicating values while preserving order."""
        merged: dict[str, list[str]] = {}
        for table in tables:
            for canon, originals in table.items():
                if canon not in merged:
                    merged[canon] = []
                exist = set(merged[canon])
                for oid in originals:
                    if oid not in exist:
                        merged[canon].append(oid)
                        exist.add(oid)
        return merged

    def merge_two_batches(
        batch_a, batch_b, similarity_threshold: float, temperature: float,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Merge two batch results by concatenating and re-running LLM merge."""
        ents_a, map_a = batch_a
        ents_b, map_b = batch_b

        merged_ents, new_map = merge_records_with_llm(
            records=ents_a + ents_b,
            similarity_threshold=similarity_threshold,
            temperature=temperature,
        )
        merged_mapping = merge_mapping_tables(map_a, map_b, new_map)
        return merged_ents, merged_mapping

    def process_round(
        batches, similarity_threshold: float, temperature: float, n: int,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Process a single merge round with two phases:

        1. Intra-batch merge: merge relations within each batch in parallel.
        2. Inter-batch merge: pairwise reduce batch results until one remains.
        """
        # Phase 1: intra-batch merge
        batch_results = []
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [
                ex.submit(merge_records_with_llm, batch, similarity_threshold, temperature)
                for batch in batches
            ]
            for fut in as_completed(futures):
                batch_results.append(fut.result())

        # Phase 2: pairwise inter-batch merge
        current = batch_results
        pair_workers = max(1, n // 2)
        while len(current) > 1:
            next_round = []
            pairs = []
            for i in range(0, len(current), 2):
                if i + 1 < len(current):
                    pairs.append((current[i], current[i + 1]))
                else:
                    next_round.append(current[i])

            with ThreadPoolExecutor(max_workers=min(pair_workers, len(pairs))) as ex:
                futures = [
                    ex.submit(merge_two_batches, a, b, similarity_threshold, temperature)
                    for a, b in pairs
                ]
                for fut in as_completed(futures):
                    next_round.append(fut.result())
            current = next_round

        if not current:
            return [], {}
        return current[0]

    # -- Main logic --
    embedding_dir_path = Path(embedding_dir)
    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    record_iter = iter_embedding_records(embedding_dir_path)
    round_idx = 0
    total_processed = 0
    global_mapping_table: dict[str, list[str]] = {}

    while True:
        round_records = take_n(record_iter, n * batch_size)
        if not round_records:
            break

        total_processed += len(round_records)

        batches = []
        for i in range(0, len(round_records), batch_size):
            batches.append(round_records[i : i + batch_size])

        merged, round_mapping = process_round(
            batches=batches,
            similarity_threshold=sim_threshold,
            temperature=temperature,
            n=n,
        )

        output_path = output_dir_path / f"rel_merged_round_{round_idx}.jsonl"
        write_jsonl(merged, output_path)

        mapping_path = output_dir_path / f"rel_mapping_round_{round_idx}.json"
        with mapping_path.open("w", encoding="utf-8") as f:
            json.dump(round_mapping, f, ensure_ascii=False, indent=2)

        for canonical_id, original_ids in round_mapping.items():
            if canonical_id in global_mapping_table:
                exist = set(global_mapping_table[canonical_id])
                for oid in original_ids:
                    if oid not in exist:
                        global_mapping_table[canonical_id].append(oid)
                        exist.add(oid)
            else:
                global_mapping_table[canonical_id] = list(original_ids)

        total_merged_entities = sum(len(v) for v in round_mapping.values())
        merge_operations = len(round_mapping)
        logger.info(
            "Round %d: %d in -> %d out, %d merge ops covering %d original relations, %d total processed",
            round_idx, len(round_records), len(merged),
            merge_operations, total_merged_entities, total_processed,
        )
        round_idx += 1

    global_mapping_path = output_dir_path / "rel_global_mapping_table.json"
    with global_mapping_path.open("w", encoding="utf-8") as f:
        json.dump(global_mapping_table, f, ensure_ascii=False, indent=2)

    logger.info(
        "Merge pipeline complete: %d records processed, %d rounds. Global mapping saved to %s",
        total_processed, round_idx, global_mapping_path,
    )


def run_rel_milvus_dedup(
    input_data_dir: str,
    output_data_dir: str,
    use_llm,
    merge_rel_prompt,
    dataset,
    emb_cfg,
    top_k: int,
    batch_size: int,
    max_workers: int,
    temperature: float,
    llm_max_retries: int,
    sync_pg: bool = True
) -> None:
    """Write KG relations to Milvus and deduplicate against existing records.

    For each batch of merged relations, searches Milvus for similar records,
    uses LLM to decide on merges, then performs batch insert/delete operations
    on both Milvus and PostgreSQL.
    """

    def read_jsonl_file(file_path: Path) -> list[dict[str, Any]]:
        """Read a JSONL file and return a list of parsed records."""
        records = []
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("Skipping invalid JSON line: %s", e)
        return records

    def write_jsonl_file(records: list[dict[str, Any]], file_path: Path):
        """Write records to a JSONL file."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def convert_relation_for_llm(rec: dict[str, Any]) -> dict[str, Any]:
        """Convert a relation record to the format expected by the LLM merge prompt."""
        return {
            "Node1": rec.get("Node1", "") or rec.get("node1", ""),
            "Node2": rec.get("Node2", "") or rec.get("node2", ""),
            "Relation": rec.get("Relation", "") or rec.get("relation", ""),
            "Type": rec.get("Type", "") or rec.get("type", ""),
            "Description": rec.get("Description", "") or rec.get("description", ""),
            "Id": rec.get("Id", "") or rec.get("id", ""),
            "chunk_id": rec.get("chunk_id", "") or rec.get("ChunkId", "") or rec.get("chunkid", ""),
        }

    def llm_merge_relations(
        records: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Use LLM to merge a list of relations and return (merged, mapping_table)."""
        if len(records) <= 1:
            return records, {}

        rels_for_prompt = [convert_relation_for_llm(r) for r in records]
        relations_json = json.dumps(rels_for_prompt, ensure_ascii=False, indent=2)
        prompt = merge_rel_prompt.format(relations_json=relations_json)

        parsed = None
        for _ in range(llm_max_retries):
            resp = use_llm(
                prompt=prompt,
                response_format={"type": "json_object"},
                temperature=temperature,
            )
            if not resp or resp == "Error":
                continue
            parsed = _parse_llm_response(resp, logger)
            if parsed:
                break

        if not parsed or not isinstance(parsed, dict):
            logger.warning("LLM merge failed, keeping relations as-is")
            return records, {}

        rel_list = parsed.get("relation_list", [])
        mapping_table = parsed.get("mapping_table", {}) or {}
        if not isinstance(mapping_table, dict) or not mapping_table or not isinstance(rel_list, list):
            return records, {}

        # Normalize mapping_table: {(n1,n2): [(o1,o2), ...]}
        # JSON keys are always strings, so we parse "Node1||Node2" format
        normalized_mapping: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for k, v in mapping_table.items():
            canon = _parse_node_pair_key(k)
            if canon is None or not isinstance(v, list):
                continue
            originals = []
            for item in v:
                pair = _parse_node_pair_value(item)
                if pair is not None:
                    originals.append(pair)
            if originals:
                normalized_mapping[canon] = originals

        if not normalized_mapping:
            return records, {}

        # Build Node1+Node2 -> original records index
        index_n1n2: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for rec in records:
            key = (normalize_name(rec.get("Node1", "")), normalize_name(rec.get("Node2", "")))
            index_n1n2.setdefault(key, []).append(rec)

        # Build index for merge_tag=true entries from LLM result
        merged_rel_info: dict[tuple[str, str], dict[str, Any]] = {}
        for rel in rel_list:
            if isinstance(rel, dict) and rel.get("merge_tag"):
                key = (normalize_name(rel.get("Node1", "")), normalize_name(rel.get("Node2", "")))
                merged_rel_info[key] = rel

        merged_output: list[dict[str, Any]] = []
        mapping: dict[str, list[str]] = {}
        used_pairs: set = set()

        for canon_pair, original_pairs in normalized_mapping.items():
            canon_n1, canon_n2 = canon_pair
            rel_tpl = merged_rel_info.get(canon_pair)
            if not rel_tpl:
                continue

            relation = rel_tpl.get("Relation", "")
            rtype = rel_tpl.get("Type", "")
            desc = rel_tpl.get("Description", "")
            attr = rel_tpl.get("Attr", {}) or {}
            if not desc:
                continue

            related_recs: list[dict[str, Any]] = []
            source_ids: list[str] = []
            for op in original_pairs:
                recs = index_n1n2.get(op, [])
                for r in recs:
                    related_recs.append(r)
                    sid = r.get("Id")
                    if sid:
                        source_ids.append(str(sid))
            if not related_recs:
                continue

            # Merge chunk_ids
            chunk_ids: list[str] = []
            seen: set = set()
            for r in related_recs:
                cid = r.get("chunk_id")
                if isinstance(cid, list):
                    for v in cid:
                        if v and v not in seen:
                            seen.add(v)
                            chunk_ids.append(v)
                elif cid:
                    if cid not in seen:
                        seen.add(cid)
                        chunk_ids.append(cid)

            new_id = get_sha256_hash(desc or f"{canon_n1}-{relation}-{canon_n2}")
            new_embedding = embedding(desc or f"{canon_n1}-{relation}-{canon_n2}", **emb_cfg)[0]

            merged_rec = {
                "Id": new_id,
                "Node1": canon_n1,
                "Node2": canon_n2,
                "Relation": relation,
                "Type": rtype,
                "Description": desc,
                "Attr": attr,
                "chunk_id": chunk_ids,
                "embedding": new_embedding,
            }
            merged_output.append(merged_rec)
            if len(source_ids) >= 2:
                mapping[new_id] = list(source_ids)
            used_pairs.update(original_pairs)

        # Pass through unmapped relations
        for key, recs in index_n1n2.items():
            if key in used_pairs:
                continue
            for rec in recs:
                merged_output.append(rec)

        if not merged_output:
            return records, {}

        return merged_output, mapping

    def process_rel_batch_with_milvus(
        collection: Collection,
        batch_records: list[dict[str, Any]],
        top_k: int,
        llm_max_workers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, list[str]]]:
        """Process a batch of relation records with Milvus-based similarity dedup.

        Returns:
            (merged_records, no_merge_records, ids_to_delete, mapping_table)
        """
        to_merge: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        no_merge: list[dict[str, Any]] = []

        logger.info("Processing relation batch of %d records", len(batch_records))

        def _process_single(record: dict[str, Any]):
            emb = record.get("embedding", [])
            if not emb:
                return "no_merge", record, []

            similars_raw = search_similar_relations(collection, emb, top_k)
            if not similars_raw:
                return "no_merge", record, []

            similars = [rel_milvus_to_record_format(r) for r in similars_raw]
            cluster = [record] + similars

            try:
                _, mapping = llm_merge_relations(cluster)
                if not mapping:
                    logger.info(
                        "no_merge | Id=%s | (%s, %s, %s)",
                        record.get("Id", ""), record.get("Node1", ""),
                        record.get("Node2", ""), record.get("Relation", ""),
                    )
                    return "no_merge", record, []

                required_ids = set()
                for _, orig_ids in mapping.items():
                    for oid in orig_ids:
                        required_ids.add(str(oid))
                filtered_similars = [
                    sr for sr in similars if str(sr.get("Id", "")) in required_ids
                ]
                logger.info(
                    "merge | Id=%s | (%s, %s, %s) | matched=%d",
                    record.get("Id", ""), record.get("Node1", ""),
                    record.get("Node2", ""), record.get("Relation", ""),
                    len(filtered_similars),
                )
                return "merge", record, filtered_similars
            except Exception as e:
                logger.error("LLM processing failed: %s", e)
                return "no_merge", record, []

        workers = min(llm_max_workers, max(1, len(batch_records)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_process_single, rec) for rec in batch_records]
            for fut in as_completed(futures):
                res_type, rec, similars = fut.result()
                if res_type == "merge":
                    to_merge.append((rec, similars))
                else:
                    no_merge.append(rec)

        ids_to_delete: set[str] = set()

        def add_group(groups, new_records):
            """Merge new records into existing groups by overlapping Ids."""
            new_ids = {str(r.get("Id", "")) for r in new_records if r.get("Id")}
            merged_indices = []
            for idx, (gid_set, _) in enumerate(groups):
                if gid_set & new_ids:
                    merged_indices.append(idx)
            if not merged_indices:
                groups.append((new_ids, new_records.copy()))
                return
            base_idx = merged_indices[0]
            base_ids, base_recs = groups[base_idx]
            base_ids |= new_ids
            for r in new_records:
                if r not in base_recs:
                    base_recs.append(r)
            for idx in reversed(merged_indices[1:]):
                gid_set, recs = groups.pop(idx)
                base_ids |= gid_set
                for r in recs:
                    if r not in base_recs:
                        base_recs.append(r)

        groups: list[tuple[set[str], list[dict[str, Any]]]] = []
        for current_rec, similar_recs in to_merge:
            for sr in similar_recs:
                sr_id = sr.get("Id", "")
                if sr_id:
                    ids_to_delete.add(sr_id)
            new_group = [current_rec] + similar_recs
            add_group(groups, new_group)

        merge_groups = [recs for _, recs in groups]
        final_merged: list[dict[str, Any]] = []
        mapping_agg: dict[str, list[str]] = {}

        def _merge_group(group_recs):
            seen_ids: set[str] = set()
            dedup_group = []
            original_ids = []
            for r in group_recs:
                rid = str(r.get("Id", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    dedup_group.append(r)
                    original_ids.append(rid)
            if len(dedup_group) <= 1:
                return dedup_group, {}, []
            try:
                merged, mt = llm_merge_relations(dedup_group)
                if mt:
                    merged_ids = {str(r.get("Id", "")) for r in merged}
                    to_remove = [oid for oid in original_ids if oid not in merged_ids]
                    return merged, mt, to_remove
                return merged, mt, []
            except Exception as e:
                logger.error("Merge group processing failed: %s", e)
                return dedup_group, {}, []

        workers_merge = min(llm_max_workers, max(1, len(merge_groups)))
        with ThreadPoolExecutor(max_workers=workers_merge) as ex:
            futures = [ex.submit(_merge_group, g) for g in merge_groups]
            for fut in as_completed(futures):
                merged, mt, ids_to_remove = fut.result()
                for rid in ids_to_remove:
                    ids_to_delete.add(rid)
                final_merged.extend(merged)
                for k, v in mt.items():
                    if k in mapping_agg:
                        exist = set(mapping_agg[k])
                        for oid in v:
                            if oid not in exist:
                                mapping_agg[k].append(oid)
                    else:
                        mapping_agg[k] = v

        return final_merged, no_merge, list(ids_to_delete), mapping_agg

    # -- Main logic --
    connect_milvus()
    collection_name = f"{dataset}_relation_collection"
    chunk_table = f"{dataset}_chunks"

    if sync_pg:
        create_graph_tables(dataset)

    collection = ensure_rel_collection(collection_name)

    def save_cluster_chunk_relations(records: list[dict[str, Any]], entity_type: str = "relation") -> None:
        """Extract chunk_ids from relation records and save to cluster_chunk_relation table."""
        if not sync_pg or not records:
            logger.info(
                "[%s] Skipping cluster_chunk_relation save: sync_pg=%s, records=%d",
                entity_type, sync_pg, len(records) if records else 0,
            )
            return

        all_chunk_ids: set[str] = set()
        cluster_chunk_mapping: dict[str, list] = {}

        for record in records:
            cluster_id = record.get("Id", "")
            if not cluster_id:
                continue

            chunk_ids = record.get("chunk_id", [])
            if isinstance(chunk_ids, str):
                try:
                    chunk_ids = json.loads(chunk_ids.replace("'", '"'))
                except (json.JSONDecodeError, ValueError):
                    chunk_ids = [cid.strip().strip("[]'\"") for cid in chunk_ids.split(",") if cid.strip()]
            elif not isinstance(chunk_ids, list):
                chunk_ids = []

            if chunk_ids:
                cluster_chunk_mapping[cluster_id] = chunk_ids
                all_chunk_ids.update(chunk_ids)

        logger.info(
            "[%s] Collected %d unique chunk_ids from %d clusters",
            entity_type, len(all_chunk_ids), len(cluster_chunk_mapping),
        )

        if not all_chunk_ids:
            logger.warning("[%s] No chunk_ids found, cannot save cluster_chunk_relation", entity_type)
            return

        chunk_source_map = get_chunk_source_mapping(list(all_chunk_ids), chunk_table)
        logger.info("[%s] Retrieved %d source mappings from chunk table", entity_type, len(chunk_source_map))

        relations = []
        for cluster_id, chunk_ids in cluster_chunk_mapping.items():
            for chunk_id in chunk_ids:
                url = chunk_source_map.get(chunk_id, "")
                if url:
                    relations.append({
                        "cluster_id": cluster_id,
                        "chunk_id": chunk_id,
                        "url": url,
                        "type": entity_type,
                        "meta": {},
                    })

        if relations:
            insert_cluster_chunk_relations(relations, dataset)
            logger.info("[%s] Saved %d cluster-chunk relations to PG", entity_type, len(relations))
        else:
            logger.warning("[%s] No valid cluster-chunk relations to save", entity_type)

    data_dir_path = Path(input_data_dir)
    output_data_dir_path = Path(output_data_dir)
    output_data_dir_path.mkdir(parents=True, exist_ok=True)

    skip_round_0 = False

    if collection.num_entities == 0:
        logger.info("Relation collection is empty, initializing...")
        init_file = data_dir_path / "rel_merged_round_0.jsonl"
        if init_file.exists():
            skip_round_0 = True
            init_records = read_jsonl_file(init_file)
            logger.info("Read %d records from init file", len(init_records))
            insert_relations_to_milvus(collection, init_records)
            if sync_pg:
                insert_relations_to_pg(init_records, dataset)
                save_cluster_chunk_relations(init_records, "relation")
            logger.info("Initialization complete")

            output_file = output_data_dir_path / "rel_merged_round_0_dedup.jsonl"
            records_without_chunk_id = [{k: v for k, v in rec.items() if k != "chunk_id"} for rec in init_records]
            write_jsonl_file(records_without_chunk_id, output_file)
            logger.info("Round 0 results saved to %s", output_file)

            mapping_file = output_data_dir_path / "rel_merged_round_0_dedup_mapping.json"
            try:
                with mapping_file.open("w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=2)
                logger.info("Round 0 mapping saved to %s", mapping_file)
            except Exception as e:
                logger.error("Failed to save round 0 mapping: %s", e)
        else:
            logger.warning("Init file not found: %s", init_file)
    else:
        logger.info("Relation collection exists with %d records", collection.num_entities)

    round_files = sorted(data_dir_path.glob("rel_merged_round_*.jsonl"))
    if skip_round_0:
        round_files = [f for f in round_files if f.name != "rel_merged_round_0.jsonl"]
    logger.info("Found %d files to process", len(round_files))

    for round_file in round_files:
        logger.info("Processing file: %s", round_file.name)
        records = read_jsonl_file(round_file)
        logger.info("Read %d records", len(records))

        all_final_records: list[dict[str, Any]] = []
        mapping_agg_all: dict[str, list[str]] = {}
        all_ids_to_delete: set[str] = set()

        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            merged, no_merge_batch, ids_to_delete, mapping_agg = process_rel_batch_with_milvus(
                collection, batch, top_k=top_k, llm_max_workers=max_workers,
            )
            all_final_records.extend(merged)
            all_final_records.extend(no_merge_batch)
            all_ids_to_delete.update(ids_to_delete)
            for k, v in mapping_agg.items():
                if k in mapping_agg_all:
                    exist = set(mapping_agg_all[k])
                    for oid in v:
                        if oid not in exist:
                            mapping_agg_all[k].append(oid)
                else:
                    mapping_agg_all[k] = v

        ids_to_delete_list = list(all_ids_to_delete)

        if ids_to_delete_list:
            logger.info("Deleting %d old relation records", len(ids_to_delete_list))
            delete_relations_from_milvus(collection, ids_to_delete_list)
            if sync_pg:
                delete_relations_from_pg(ids_to_delete_list, dataset)
                delete_cluster_chunk_relations_by_cluster_ids(ids_to_delete_list, dataset)

        if all_final_records:
            logger.info("Inserting %d final relation records", len(all_final_records))
            insert_relations_to_milvus(collection, all_final_records)
            if sync_pg:
                insert_relations_to_pg(all_final_records, dataset)
                save_cluster_chunk_relations(all_final_records, "relation")

        output_file = output_data_dir_path / f"{round_file.stem}_dedup.jsonl"
        records_without_chunk_id = [{k: v for k, v in rec.items() if k != "chunk_id"} for rec in all_final_records]
        write_jsonl_file(records_without_chunk_id, output_file)
        logger.info("Results saved to %s", output_file)

        mapping_output_file = output_data_dir_path / f"{round_file.stem}_dedup_mapping.json"
        try:
            with mapping_output_file.open("w", encoding="utf-8") as f:
                json.dump(mapping_agg_all, f, ensure_ascii=False, indent=2)
            logger.info("Mapping saved to %s", mapping_output_file)
        except Exception as e:
            logger.error("Failed to save mapping: %s", e)

    logger.info("All relation processing complete")
