"""Node deduplication and merging for knowledge graph construction.

Provides multi-round LLM-based deduplication, embedding generation,
batch clustering + merge pipeline, and Milvus-backed global deduplication.
"""

import itertools
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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

from ..sql_db.sql_db import (
    create_graph_tables,
    delete_entities_from_pg,
    insert_entities_to_pg,
    insert_cluster_chunk_relations,
    delete_cluster_chunk_relations_by_cluster_ids,
    get_chunk_source_mapping,
)
from ..vector_db.vector_db import (
    ensure_nodes_collection,
    connect_milvus,
    insert_nodes_records_to_milvus,
    delete_nodes_records_from_milvus,
    search_similar_entities,
)

logger = logging.getLogger(__name__)
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


def read_nodes(input_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Stream JSONL and collect duplicates keyed by NodeName."""
    uniques: dict[str, dict[str, Any]] = {}
    duplicates: dict[str, list[dict[str, Any]]] = defaultdict(list)

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

            node_name = normalize_name(record.get("NodeName", ""))
            if not node_name:
                logger.warning("Line %s missing NodeName, skipped", line_no)
                continue

            if node_name in uniques:
                if node_name not in duplicates:
                    duplicates[node_name].append(uniques[node_name])
                duplicates[node_name].append(record)
            else:
                uniques[node_name] = record

    logger.info("Scan complete: %d unique entities, %d duplicate groups", len(uniques), len(duplicates))
    return uniques, duplicates


def split_uniques_duplicates_from_records(
    records: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Re-partition in-memory records into unique and duplicate groups by NodeName."""
    uniques: dict[str, dict[str, Any]] = {}
    duplicates: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for rec in records:
        node_name = normalize_name(rec.get("NodeName", ""))
        if not node_name:
            continue
        if node_name in uniques:
            if node_name not in duplicates:
                duplicates[node_name].append(uniques[node_name])
            duplicates[node_name].append(rec)
        else:
            uniques[node_name] = rec

    return uniques, duplicates


def build_prompt(node_name: str, entities: list[dict[str, Any]], prompt_template) -> str:
    """Build an LLM prompt from entities and a template."""
    entity_block = json.dumps(entities, ensure_ascii=False, indent=2)
    return prompt_template.format(node_name=node_name, entity_block=entity_block)






def dedup_by_llm(
    use_llm, node_name: str, entities: list[dict[str, Any]], dedup_template: str,
    llm_batch_size: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Iterative LLM-based dedup: process entities in batches until fully merged."""

    split_entities: list[dict[str, Any]] = []

    def _select_main_and_split(parsed_resp: Any) -> dict[str, Any]:
        """Select the main entity from parsed response; extras go to split_entities."""
        if isinstance(parsed_resp, dict):
            return parsed_resp

        if isinstance(parsed_resp, list):
            main_entity: dict[str, Any] | None = None
            for item in parsed_resp:
                if not isinstance(item, dict):
                    continue
                item_name = normalize_name(item.get("NodeName", ""))
                if main_entity is None and item_name == normalize_name(node_name):
                    main_entity = item
                else:
                    split_entities.append(item)

            if main_entity is None:
                for item in parsed_resp:
                    if isinstance(item, dict):
                        main_entity = item
                        break

            return main_entity or {}

        logger.error("Unhandled LLM response type: %s", type(parsed_resp))
        return {}

    if len(entities) <= llm_batch_size:
        resp_str = use_llm(prompt=build_prompt(node_name, entities, dedup_template))
        parsed = _parse_llm_response(resp_str, logger)
        main_entity = _select_main_and_split(parsed)
        return main_entity, split_entities

    accumulated_result = None
    total_batches = (len(entities) + llm_batch_size - 1) // llm_batch_size

    for batch_idx in range(total_batches):
        start_idx = batch_idx * llm_batch_size
        end_idx = min(start_idx + llm_batch_size, len(entities))
        batch_entities = entities[start_idx:end_idx]

        logger.info(
            "Processing %s batch %d/%d (%d entities)",
            node_name, batch_idx + 1, total_batches, len(batch_entities),
        )

        if accumulated_result is not None:
            entities_to_merge = [accumulated_result] + batch_entities
        else:
            entities_to_merge = batch_entities

        resp_str = use_llm(prompt=build_prompt(node_name, entities_to_merge, dedup_template))
        parsed = _parse_llm_response(resp_str, logger)
        accumulated_result = _select_main_and_split(parsed)

        if not accumulated_result:
            logger.error(
                "Parse failed for %s batch %d/%d, empty result",
                node_name, batch_idx + 1, total_batches,
            )

    return (accumulated_result if accumulated_result is not None else {}, split_entities)


def dedup_nodes(
    use_llm,
    dedup_template: str,
    input_path: Path,
    output_path: Path,
    workers: int,
    max_rounds: int,
    llm_batch_size: int,
) -> None:
    """Multi-round LLM-based entity node deduplication.

    Groups entities by NodeName, merges duplicates via LLM, handles entity
    splits, and iterates until no duplicates remain or *max_rounds* is reached.
    splits, and iterates until no duplicates remain.

    Args:
        use_llm: LLM callable for merge decisions.
        dedup_template: Prompt template for dedup.
        input_path: Input JSONL file with entity nodes.
        output_path: Output JSONL file for deduplicated entities.
        workers: Number of parallel threads.
    """
    uniques, duplicates = read_nodes(input_path)

    if not duplicates:
        logger.info("No duplicate entities found, copying input to output")
        write_jsonl(uniques.values(), output_path)
        return

    current_uniques, current_duplicates = uniques, duplicates
    output_records: list[dict[str, Any]] = list(current_uniques.values())

    total_rounds = 0
    total_groups = 0
    total_main = 0
    total_split = 0

    def _merge_task(node_name: str, records: list[dict[str, Any]], dedup_template: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """Dedup-merge a single group of same-name entities."""
        merged, split_entities = dedup_by_llm(use_llm, node_name, records, dedup_template, llm_batch_size=llm_batch_size)

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

        _add_chunk(merged.get("ChunkId"))

        if chunk_ids:
            merged["chunk_id"] = sorted(chunk_ids)

        desc_text = str(merged.get("Description", ""))
        merged["Id"] = get_sha256_hash(desc_text)

        for split in split_entities:
            desc_text_split = str(split.get("Description", ""))
            split["Id"] = get_sha256_hash(desc_text_split)

        return node_name, merged, split_entities

    while current_duplicates and total_rounds < max_rounds:
        total_rounds += 1
        logger.info("Starting dedup round %d, %d duplicate groups", total_rounds, len(current_duplicates))

        merged_results: dict[str, dict[str, Any]] = {}
        extra_entities: list[dict[str, Any]] = []
        round_stats: dict[str, dict[str, int]] = {}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_name = {
                executor.submit(_merge_task, name, records, dedup_template): name
                for name, records in current_duplicates.items()
            }

            total = len(future_to_name)
            for idx, future in enumerate(as_completed(future_to_name), 1):
                name = future_to_name[future]
                try:
                    node_name, merged, split_entities = future.result()

                    merged_results[node_name] = merged
                    extra_entities.extend(split_entities)

                    round_stats[name] = {
                        "merged": 1 if merged else 0,
                        "split": len(split_entities),
                    }

                    logger.info(
                        "(%d/%d) Merged duplicate entity: %s (%d splits)",
                        idx, total, name, len(split_entities),
                    )

                except Exception as exc:
                    logger.error("Failed to merge entity %s: %s", name, exc)

        output_records = []
        for name, record in current_uniques.items():
            if name in merged_results:
                output_records.append(merged_results[name])
            else:
                output_records.append(record)

        output_records.extend(extra_entities)

        if round_stats:
            round_split = sum(v["split"] for v in round_stats.values())
            round_main = sum(v["merged"] for v in round_stats.values())

            logger.info(
                "Round %d stats: %d groups, %d main entities, %d splits",
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
            "Dedup summary: %d rounds, %d groups, %d main entities, %d splits",
            total_rounds, total_groups, total_main, total_split,
        )
    logger.info("Dedup complete, output: %s, %d records", output_path, len(output_records))

def embed_nodes(
    api_key: str,
    embedding_url: str,
    embedding_model: str,
    embedding_timeout: int,
    nodes_input_path: str,
    output_dir: str,
    batch_size: int,
    max_file_size_bytes: int,
    num_threads: int,
    max_retries: int,
    retry_delay: int,
    embedding_dim: int,
) -> int:
    """Generate embeddings for deduplicated nodes and write to JSONL files."""
    logger.info("Starting node embedding process")
    ok = check_embedding_connection(
        api_key, embedding_url, embedding_model, embedding_timeout
    )
    if not ok:
        logger.error("Embedding API test failed")
        return 0

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    file_manager_attr = FileManager(
        output_dir, "nodes", max_size_bytes=max_file_size_bytes, start_index=0
    )
    attr_count = process_file(
        nodes_input_path,
        "nodes",
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
    logger.info("Processed %d node records", attr_count)
    return attr_count





def run_merge_pipeline(
    embedding_dir: str,
    output_dir: str,
    use_llm,
    emb_cfg,
    merge_cluster_prompt,
    batch_size: int,
    n: int,
    sim_threshold: float,
    temperature: float,
    max_workers: int,
    llm_max_retries: int,
):
    """Batch clustering + LLM merge pipeline for deduplicated node embeddings.

    Reads embedding records in batches of ``n * batch_size``, clusters them
    by cosine similarity, merges similar entities via LLM, and writes per-round
    output files plus a global mapping table.

    Args:
        embedding_dir: Directory containing ``*.jsonl`` embedding files.
        output_dir: Output directory for merged results and mappings.
        use_llm: LLM callable.
        emb_cfg: Embedding API config dict.
        merge_cluster_prompt: Prompt template for cluster merging.
        batch_size: Records per batch.
        n: Number of batches per round / intra-batch concurrency.
        sim_threshold: Cosine similarity threshold for clustering.
        temperature: LLM temperature.
    """

    def convert_record_to_entity_format(record: dict[str, Any]) -> dict[str, Any]:
        """Convert an embedding record to the entity format expected by the merge prompt."""
        entity = {
            "NodeName": record.get("NodeName", ""),
            "Description": record.get("Description", ""),
            "Attr": {},
        }
        for key, value in record.items():
            if key not in ["Id", "NodeName", "Description", "chunk_id", "source_file", "embedding"]:
                entity["Attr"][key] = value
        return entity

    def llm_merge_cluster(
        use_llm,
        cluster: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Call LLM to merge/split a single cluster of similar entities.

        If mapping_table is empty, returns the original cluster unchanged.
        Otherwise re-generates Id/chunk_id/embedding for merged entities
        and passes through unmerged entities as-is.
        """
        entity_list = [convert_record_to_entity_format(rec) for rec in cluster]
        entity_list_json = json.dumps(entity_list, ensure_ascii=False, indent=2)
        prompt = merge_cluster_prompt.format(entity_list_json=entity_list_json)

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
            return list(cluster), {}

        entity_list_result = parsed.get("entity_list", [])
        mapping_table = parsed.get("mapping_table", {}) or {}

        if not mapping_table:
            logger.info("No merge detected, returning original cluster")
            return list(cluster), {}

        name_to_rec = {
            normalize_name(rec.get("NodeName", "")): rec
            for rec in cluster
        }

        def collect_chunk_ids_by_names(orig_names: list[str]) -> list[str]:
            """Aggregate and deduplicate chunk_ids from the named original entities."""
            collected = []
            for nm in orig_names:
                rec = name_to_rec.get(normalize_name(nm))
                if not rec:
                    continue
                cid = rec.get("chunk_id") or rec.get("chunk_ids")
                if not cid:
                    continue
                if isinstance(cid, list):
                    collected.extend(cid)
                else:
                    collected.append(cid)

            seen = set()
            deduped = []
            for c in collected:
                if c and c not in seen:
                    seen.add(c)
                    deduped.append(c)
            return deduped

        merged_entities: list[dict[str, Any]] = []
        used_original_names = set()

        llm_entity_map = {
            normalize_name(
                e.get("NodeName", "")
                or e.get("Nodename", "")
                or e.get("name", "")
            ): e
            for e in entity_list_result
        }

        for new_name, original_names in mapping_table.items():
            norm_new_name = normalize_name(new_name)
            if not norm_new_name:
                continue

            original_names = [
                nm for nm in original_names
                if normalize_name(nm) in name_to_rec
            ]
            if not original_names:
                continue

            for nm in original_names:
                used_original_names.add(normalize_name(nm))

            chunk_ids = collect_chunk_ids_by_names(original_names)

            llm_entity = llm_entity_map.get(norm_new_name, {})
            desc_text = (
                llm_entity.get("description")
                or llm_entity.get("Description")
                or ""
            )

            new_id = get_sha256_hash(desc_text or new_name)
            new_embedding = embedding(
                texts=desc_text or new_name,
                **emb_cfg
            )[0]

            merged_rec = llm_entity.copy()
            merged_rec.update({
                "NodeName": new_name,
                "Id": new_id,
                "chunk_id": chunk_ids,
                "embedding": new_embedding,
            })

            merged_entities.append(merged_rec)

        for norm_name, rec in name_to_rec.items():
            if norm_name not in used_original_names:
                merged_entities.append(rec.copy())

        before_names = [rec.get("NodeName", "") for rec in cluster]
        after_names = [rec.get("NodeName", "") for rec in merged_entities]
        logger.info(
            "Cluster merge applied: %d -> %d entities | %s -> %s",
            len(before_names), len(after_names), before_names, after_names,
        )

        return merged_entities, mapping_table


    def merge_records_with_llm(
        use_llm,
        records: Sequence[dict[str, Any]],
        similarity_threshold: float,
        temperature: float,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Cluster records by embedding similarity, then LLM-merge each multi-item cluster."""
        clusters = cluster_by_embedding(records, similarity_threshold)
        merged: list[dict[str, Any]] = []
        all_mappings: dict[str, list[str]] = {}

        llm_clusters: list[Sequence[dict[str, Any]]] = []
        for cluster in clusters:
            if len(cluster) == 1:
                merged.extend(cluster)
            else:
                llm_clusters.append(cluster)

        if llm_clusters:
            pool_size = min(len(llm_clusters), max(1, max_workers // n))
            with ThreadPoolExecutor(max_workers=pool_size) as ex:
                futures = [ex.submit(llm_merge_cluster, use_llm, cluster) for cluster in llm_clusters]
                for fut in as_completed(futures):
                    cluster_merged, cluster_mapping = fut.result()
                    merged.extend(cluster_merged)
                    for canonical_id, original_ids in cluster_mapping.items():
                        if canonical_id in all_mappings:
                            existing_set = set(all_mappings[canonical_id])
                            for oid in original_ids:
                                if oid not in existing_set:
                                    all_mappings[canonical_id].append(oid)
                        else:
                            all_mappings[canonical_id] = original_ids.copy()

        return merged, all_mappings

    def merge_mapping_tables(
        *tables: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        """Merge multiple mapping tables, deduplicating values while preserving order."""
        merged: dict[str, list[str]] = {}

        for table in tables:
            for canon, originals in table.items():
                if canon not in merged:
                    merged[canon] = []
                seen = set(merged[canon])
                for name in originals:
                    if name not in seen:
                        merged[canon].append(name)
                        seen.add(name)
        return merged

    def merge_two_batches(
        use_llm,
        batch_a: tuple[list[dict[str, Any]], dict[str, list[str]]],
        batch_b: tuple[list[dict[str, Any]], dict[str, list[str]]],
        similarity_threshold: float,
        temperature: float,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Merge two batch results by concatenating entities and re-running LLM merge."""
        ents_a, map_a = batch_a
        ents_b, map_b = batch_b

        merged_ents, new_map = merge_records_with_llm(
            use_llm=use_llm,
            records=ents_a + ents_b,
            similarity_threshold=similarity_threshold,
            temperature=temperature,
        )

        merged_mapping = merge_mapping_tables(map_a, map_b, new_map)
        return merged_ents, merged_mapping

    def process_round(
        use_llm,
        batches: list[list[dict[str, Any]]],
        similarity_threshold: float,
        temperature: float,
        n: int,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Process a single merge round with two phases:

        1. Intra-batch merge: merge entities within each batch in parallel.
        2. Inter-batch merge: pairwise reduce batch results until one remains.
        """
        # Phase 1: intra-batch merge
        batch_results: list[tuple[list[dict[str, Any]], dict[str, list[str]]]] = []

        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [
                ex.submit(
                    merge_records_with_llm,
                    use_llm,
                    batch,
                    similarity_threshold,
                    temperature,
                )
                for batch in batches
            ]

            for fut in as_completed(futures):
                batch_results.append(fut.result())

        # Phase 2: pairwise inter-batch merge
        current = batch_results
        pair_workers = max(1, n // 2)

        while len(current) > 1:
            next_round: list[tuple[list[dict[str, Any]], dict[str, list[str]]]] = []
            pairs = []

            for i in range(0, len(current), 2):
                if i + 1 < len(current):
                    pairs.append((current[i], current[i + 1]))
                else:
                    next_round.append(current[i])

            with ThreadPoolExecutor(
                max_workers=min(pair_workers, len(pairs)),
            ) as ex:
                futures = [
                    ex.submit(
                        merge_two_batches,
                        use_llm,
                        a,
                        b,
                        similarity_threshold,
                        temperature,
                    )
                    for a, b in pairs
                ]
                for fut in as_completed(futures):
                    next_round.append(fut.result())

            current = next_round

        if not current:
            return [], {}

        return current[0]

    # Main logic
    embedding_dir = Path(embedding_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    record_iter = iter_embedding_records(embedding_dir)
    round_idx = 0
    total_processed = 0
    global_mapping_table: dict[str, list[str]] = {}

    while True:
        round_records = take_n(record_iter, n * batch_size)
        if not round_records:
            break

        total_processed += len(round_records)
        batches: list[list[dict[str, Any]]] = []
        for i in range(0, len(round_records), batch_size):
            batches.append(round_records[i : i + batch_size])

        merged, round_mapping = process_round(
            use_llm=use_llm,
            batches=batches,
            similarity_threshold=sim_threshold,
            temperature=temperature,
            n=n,
        )

        output_path = output_dir / f"merged_round_{round_idx}.jsonl"
        write_jsonl(merged, output_path)

        mapping_path = output_dir / f"mapping_round_{round_idx}.json"
        with mapping_path.open("w", encoding="utf-8") as f:
            json.dump(round_mapping, f, ensure_ascii=False, indent=2)

        for canonical_id, original_ids in round_mapping.items():
            if canonical_id in global_mapping_table:
                existing_ids = set(global_mapping_table[canonical_id])
                for oid in original_ids:
                    if oid not in existing_ids:
                        global_mapping_table[canonical_id].append(oid)
            else:
                global_mapping_table[canonical_id] = original_ids.copy()

        total_merged_entities = sum(len(v) for v in round_mapping.values())
        merge_operations = len(round_mapping)

        logger.info(
            "Round %d: %d in -> %d out, %d merge ops covering %d original entities, "
            "%d total processed",
            round_idx, len(round_records), len(merged),
            merge_operations, total_merged_entities, total_processed,
        )
        round_idx += 1

    global_mapping_path = output_dir / "global_mapping_table.json"
    with global_mapping_path.open("w", encoding="utf-8") as f:
        json.dump(global_mapping_table, f, ensure_ascii=False, indent=2)

    logger.info(
        "Merge pipeline complete: %d records processed, %d rounds. "
        "Global mapping saved to %s",
        total_processed, round_idx, global_mapping_path,
    )






def run_milvus_dedup(
    input_data_dir: str,
    output_data_dir: str,
    use_llm,
    merge_cluster_prompt,
    dataset,
    emb_cfg,
    top_k: int,
    batch_size: int,
    max_workers: int,
    temperature: float,
    llm_max_retries: int,
    sync_pg: bool = True,
) -> None:
    """Write KG entities to Milvus and deduplicate against existing records.

    For each batch of merged entities, searches Milvus for similar records,
    uses LLM to decide on merges, then performs batch insert/delete operations
    on both Milvus and PostgreSQL.
    """
    connect_milvus()
    collection_name = f"{dataset}_entity_collection"
    chunk_table = f"{dataset}_chunks"

    if sync_pg:
        create_graph_tables(dataset)
    collection = ensure_nodes_collection(collection_name)

    def save_cluster_chunk_relations(records: list[dict[str, Any]], entity_type: str = "entity") -> None:
        """Extract chunk_ids from entity records and save to cluster_chunk_relation table."""
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

    def record_to_milvus_format(record: dict[str, Any]) -> dict[str, Any]:
        """Convert a JSONL record to Milvus insert format."""
        entity_id = record.get("Id", "")
        chunk_ids = record.get("chunk_id", [])
        if isinstance(chunk_ids, list):
            valid_chunk_ids = [str(cid).strip() for cid in chunk_ids if cid]
            chunk_id_str = ",".join(valid_chunk_ids) if valid_chunk_ids else ""
        elif isinstance(chunk_ids, str):
            chunk_id_str = chunk_ids.strip() if chunk_ids.strip() else ""
        else:
            chunk_id_str = str(chunk_ids).strip() if chunk_ids else ""

        attr_dict = record.get("Attr", {})
        attr_str = json.dumps(attr_dict, ensure_ascii=False) if attr_dict else "{}"

        emb = record.get("embedding", [])
        if not isinstance(emb, list):
            emb = []

        return {
            "id": entity_id,
            "chunk_id": chunk_id_str,
            "nodename": record.get("NodeName", ""),
            "description": record.get("Description", ""),
            "type": record.get("Type", ""),
            "subtype": record.get("SubType", ""),
            "attr": attr_str,
            "embedding": emb,
        }

    def milvus_to_record_format(milvus_data: dict[str, Any]) -> dict[str, Any]:
        """Convert Milvus search result back to JSONL record format."""
        chunk_id_str = milvus_data.get("chunk_id", "")
        if chunk_id_str:
            chunk_ids = [cid.strip() for cid in chunk_id_str.split(",") if cid.strip()]
        else:
            chunk_ids = []

        attr_str = milvus_data.get("attr", "{}")
        try:
            attr_dict = json.loads(attr_str) if attr_str else {}
        except (json.JSONDecodeError, ValueError):
            attr_dict = {}

        return {
            "Id": milvus_data.get("id", ""),
            "chunk_id": chunk_ids,
            "NodeName": milvus_data.get("nodename", ""),
            "Description": milvus_data.get("description", ""),
            "Type": milvus_data.get("type", ""),
            "SubType": milvus_data.get("subtype", ""),
            "Attr": attr_dict,
            "embedding": milvus_data.get("embedding", []),
        }





    def convert_to_entity_format_for_llm(record: dict[str, Any]) -> dict[str, Any]:
        """Convert a record to the entity format expected by the LLM merge prompt."""
        attr_data = record.get("Attr") or (json.loads(record.get("attr", "{}")) if isinstance(record.get("attr"), str) else {})
        if isinstance(attr_data, dict) and "chunk_id" in attr_data:
            attr_data = {k: v for k, v in attr_data.items() if k != "chunk_id"}

        return {
            "NodeName": record.get("NodeName") or record.get("nodename", ""),
            "Description": record.get("Description") or record.get("description", ""),
            "Attr": attr_data,
        }

    def llm_merge_entities(entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Use LLM to merge a list of entities and return (merged_entities, mapping_table)."""
        if len(entities) <= 1:
            return entities, {}

        entity_list = [convert_to_entity_format_for_llm(e) for e in entities]
        entity_list_json = json.dumps(entity_list, ensure_ascii=False, indent=2)
        prompt = merge_cluster_prompt.format(entity_list_json=entity_list_json)

        parsed = None
        for attempt in range(llm_max_retries):
            resp = use_llm(
                prompt=prompt,
                response_format={"type": "json_object"},
                temperature=temperature,
            )
            if not resp or resp == "Error":
                continue
            parsed = _parse_llm_response(resp, logger)
            if parsed and isinstance(parsed, dict):
                break

        if not parsed or not isinstance(parsed, dict):
            logger.warning("LLM merge failed, keeping entities as-is")
            return entities, {}

        entity_list_result = parsed.get("entity_list", [])
        mapping_table = parsed.get("mapping_table", {})
        if mapping_table is None:
            return entities, {}

        merged_entities = []
        name_to_rec = {normalize_name(rec.get("NodeName", "") or rec.get("nodename", "")): rec for rec in entities}

        for entity in entity_list_result:
            entity_name = entity.get("NodeName", "")
            merge_tag = entity.get("merge_tag", False)
            norm_name = normalize_name(entity_name)

            if not norm_name:
                continue

            if merge_tag:
                original_names = mapping_table.get(entity_name, [entity_name])
                original_names = [nm for nm in original_names if normalize_name(nm) in name_to_rec] or [entity_name]

                chunk_ids = []
                for nm in original_names:
                    rec = name_to_rec.get(normalize_name(nm))
                    if rec:
                        cids = rec.get("chunk_id")
                        if cids is None:
                            continue
                        elif isinstance(cids, list):
                            chunk_ids.extend([cid for cid in cids if cid])
                        elif isinstance(cids, str):
                            if cids.strip():
                                chunk_ids.extend([cid.strip() for cid in cids.split(",") if cid.strip()])
                        else:
                            cid_str = str(cids).strip()
                            if cid_str:
                                chunk_ids.extend([cid.strip() for cid in cid_str.split(",") if cid.strip()])

                seen = set()
                deduped_chunk_ids = []
                for cid in chunk_ids:
                    cid = str(cid).strip()
                    if cid and cid not in seen:
                        seen.add(cid)
                        deduped_chunk_ids.append(cid)

                desc_text = entity.get("description", "") or entity.get("Description", "")
                new_id = get_sha256_hash(desc_text or entity_name or (original_names[0] if original_names else ""))

                new_embedding = embedding(desc_text or entity_name or (original_names[0] if original_names else ""), **emb_cfg)[0]
                merged_rec = {
                    "Id": new_id,
                    "NodeName": entity_name,
                    "Description": entity.get("description", "") or entity.get("Description", ""),
                    "Type": entity.get("type", "") or (entities[0].get("Type", "") if entities else ""),
                    "SubType": entity.get("subtype", "") or (entities[0].get("SubType", "") if entities else ""),
                    "Attr": entity.get("Attr", {}),
                    "chunk_id": deduped_chunk_ids,
                    "embedding": new_embedding,
                }
                merged_entities.append(merged_rec)
            else:
                base_rec = name_to_rec.get(norm_name)
                if base_rec:
                    merged_rec = base_rec.copy()
                    merged_entities.append(merged_rec)

        return merged_entities, mapping_table

    def process_batch_with_milvus(
        batch_records: list[dict[str, Any]],
        top_k: int,
        llm_max_workers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, list[str]]]:
        """Process a batch of entity records with Milvus-based similarity dedup.

        For each record, searches Milvus for similar entities, uses LLM to
        decide merges, groups overlapping merge sets, then performs final
        LLM merge on each group.

        Returns:
            (merged_records, no_merge_records, ids_to_delete, mapping_table)
        """
        to_merge: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        no_merge: list[dict[str, Any]] = []

        logger.info("Processing batch of %d records", len(batch_records))

        def _process_single(record: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
            emb = record.get("embedding", [])
            if not emb:
                return ("no_merge", record, [])

            similar_records = search_similar_entities(collection, emb, top_k)
            if not similar_records:
                return ("no_merge", record, [])

            similar_records_formatted = [milvus_to_record_format(sr) for sr in similar_records]
            entities_for_llm = [record] + similar_records_formatted

            try:
                _, mapping_table = llm_merge_entities(entities_for_llm)
                has_merge = bool(mapping_table)
                if not has_merge:
                    logger.info(
                        "no_merge | Id=%s | NodeName=%s",
                        record.get("Id", ""), record.get("NodeName", ""),
                    )
                    return ("no_merge", record, [])

                required_names = set()
                for _, orig_names in mapping_table.items():
                    for nm in orig_names:
                        required_names.add(normalize_name(nm))
                filtered_similars = [
                    sr for sr in similar_records_formatted
                    if normalize_name(sr.get("NodeName", "")) in required_names
                ]
                logger.info(
                    "merge | Id=%s | NodeName=%s | matched=%d",
                    record.get("Id", ""), record.get("NodeName", ""),
                    len(filtered_similars),
                )
                return ("merge", record, filtered_similars)
            except Exception as e:
                logger.error("LLM processing failed: %s", e)
                return ("no_merge", record, [])

        workers = min(llm_max_workers, max(1, len(batch_records)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_process_single, rec) for rec in batch_records]
            for fut in as_completed(futures):
                result_type, rec, similars = fut.result()
                if result_type == "merge":
                    to_merge.append((rec, similars))
                else:
                    no_merge.append(rec)

        ids_to_delete: set[str] = set()

        def add_group(groups: list[tuple[set[str], list[dict[str, Any]]]], new_records: list[dict[str, Any]]):
            """Merge new records into existing groups by overlapping Ids."""
            new_ids = {rec.get("Id", "") for rec in new_records if rec.get("Id", "")}
            merged_indices = []
            for idx, (gid_set, _) in enumerate(groups):
                if gid_set & new_ids:
                    merged_indices.append(idx)
            if not merged_indices:
                groups.append((new_ids, new_records.copy()))
                return
            base_idx = merged_indices[0]
            base_ids, base_records = groups[base_idx]
            base_ids |= new_ids
            for rec in new_records:
                if rec not in base_records:
                    base_records.append(rec)
            for idx in reversed(merged_indices[1:]):
                ids_set, recs = groups.pop(idx)
                base_ids |= ids_set
                for rec in recs:
                    if rec not in base_records:
                        base_records.append(rec)

        groups: list[tuple[set[str], list[dict[str, Any]]]] = []

        for current_rec, similar_recs in to_merge:
            for sr in similar_recs:
                sr_id = sr.get("Id", "")
                if sr_id:
                    ids_to_delete.add(sr_id)
            new_group_records = [current_rec] + similar_recs
            add_group(groups, new_group_records)

        merge_groups: list[list[dict[str, Any]]] = [recs for _, recs in groups]

        final_merged: list[dict[str, Any]] = []
        mapping_agg: dict[str, list[str]] = {}

        def _merge_group(group_records: list[dict[str, Any]]):
            seen_ids = set()
            deduped_group = []
            original_ids = []
            for rec in group_records:
                rec_id = rec.get("Id", "")
                if rec_id and rec_id not in seen_ids:
                    seen_ids.add(rec_id)
                    deduped_group.append(rec)
                    original_ids.append(rec_id)
            if len(deduped_group) <= 1:
                return deduped_group, {}, []
            try:
                merged, mt = llm_merge_entities(deduped_group)
                if mt:
                    merged_ids = {rec.get("Id", "") for rec in merged}
                    ids_to_remove = [oid for oid in original_ids if oid not in merged_ids]
                    return merged, mt, ids_to_remove
                else:
                    return merged, mt, []
            except Exception as e:
                logger.error("Merge group processing failed: %s", e)
                return deduped_group, {}, []

        workers_merge = min(llm_max_workers, max(1, len(merge_groups)))
        with ThreadPoolExecutor(max_workers=workers_merge) as ex:
            futures = [ex.submit(_merge_group, grp) for grp in merge_groups]
            for fut in as_completed(futures):
                merged, mt, ids_to_remove = fut.result()

                for rid in ids_to_remove:
                    ids_to_delete.add(rid)

                final_merged.extend(merged)
                for k, v in mt.items():
                    if k in mapping_agg:
                        existing = set(mapping_agg[k])
                        for vv in v:
                            if vv not in existing:
                                mapping_agg[k].append(vv)
                    else:
                        mapping_agg[k] = v

        return final_merged, no_merge, list(ids_to_delete), mapping_agg

    # Main logic
    input_data_dir = Path(input_data_dir)
    output_data_dir = Path(output_data_dir)
    skip_round_0 = False

    if collection.num_entities == 0:
        logger.info("Collection is empty, initializing...")
        init_file = input_data_dir / "merged_round_0.jsonl"
        if init_file.exists():
            skip_round_0 = True
            init_records = read_jsonl_file(init_file)
            logger.info("Read %d records from init file", len(init_records))
            insert_nodes_records_to_milvus(collection, init_records)
            if sync_pg:
                insert_entities_to_pg(init_records, dataset)
                save_cluster_chunk_relations(init_records, "entity")
            logger.info("Initialization complete")

            output_file = output_data_dir / "merged_round_0_dedup.jsonl"
            records_without_chunk_id = [{k: v for k, v in rec.items() if k != "chunk_id"} for rec in init_records]
            write_jsonl_file(records_without_chunk_id, output_file)
            logger.info("Round 0 results saved to %s", output_file)

            mapping_output_file = output_data_dir / "merged_round_0_dedup_mapping.json"
            try:
                with mapping_output_file.open("w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=2)
                logger.info("Round 0 mapping saved to %s", mapping_output_file)
            except Exception as e:
                logger.error("Failed to save round 0 mapping: %s", e)
        else:
            logger.warning("Init file not found: %s", init_file)
    else:
        logger.info("Collection exists with %d entities", collection.num_entities)

    round_files = sorted(input_data_dir.glob("merged_round_*.jsonl"))
    if skip_round_0:
        round_files = [f for f in round_files if f.name != "merged_round_0.jsonl"]
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
            merged, no_merge, ids_to_delete, mapping_agg = process_batch_with_milvus(
                batch, top_k, llm_max_workers=max_workers,
            )
            all_final_records.extend(merged)
            all_final_records.extend(no_merge)
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
            logger.info("Deleting %d old records", len(ids_to_delete_list))
            delete_nodes_records_from_milvus(collection, ids_to_delete_list)
            if sync_pg:
                delete_entities_from_pg(ids_to_delete_list, dataset)
                delete_cluster_chunk_relations_by_cluster_ids(ids_to_delete_list, dataset)

        if all_final_records:
            logger.info("Inserting %d final records", len(all_final_records))
            insert_nodes_records_to_milvus(collection, all_final_records)
            if sync_pg:
                insert_entities_to_pg(all_final_records, dataset)
                save_cluster_chunk_relations(all_final_records, "entity")

        output_file = output_data_dir / f"{round_file.stem}_dedup.jsonl"
        records_without_chunk_id = [{k: v for k, v in rec.items() if k != "chunk_id"} for rec in all_final_records]
        write_jsonl_file(records_without_chunk_id, output_file)
        logger.info("Results saved to %s", output_file)

        mapping_output_file = output_data_dir / f"{round_file.stem}_dedup_mapping.json"
        try:
            with mapping_output_file.open("w", encoding="utf-8") as f:
                json.dump(mapping_agg_all, f, ensure_ascii=False, indent=2)
            logger.info("Mapping saved to %s", mapping_output_file)
        except Exception as e:
            logger.error("Failed to save mapping: %s", e)

    logger.info("All processing complete")
