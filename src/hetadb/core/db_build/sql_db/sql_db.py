"""PostgreSQL operations for chunk, entity, relation, and cluster-chunk data.

Provides table creation, batch insert (via psycopg2 execute_values),
querying, and deletion for all HetaDB table types.  Functions in the
chunk-merge pipeline accept an explicit ``postgres_config`` dict; legacy
graph functions use ``get_postgres_connection()`` which reads from
the project config.yaml.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

from hetadb.utils.utils import clean_str, tokenize_for_tsvector
from hetadb.utils.load_config import get_postgres_conn_config

logger = logging.getLogger("hetadb.sql_db")


def get_data_paths(data_dir: str):
    """Return sub-directory paths for embeddings, chunks, nodes, and relations."""
    base_path = data_dir
    return {
        "emb_dir": os.path.join(base_path, "emb", "graph_embedding"),
        "chunk_emb_dir": os.path.join(base_path, "emb", "chunk_embeding"),
        "chunks_dir": os.path.join(base_path, "graph", "chunk"),
        "nodes_dir": os.path.join(base_path, "graph", "node"),
        "relations_dir": os.path.join(base_path, "graph", "relation"),
    }


BATCH_SIZE = 1000
NUM_THREADS = 4


def get_postgres_connection():
    """Get a PostgreSQL connection, auto-creating the database if it does not exist."""
    try:
        config = get_postgres_conn_config()
        dbname = config.get('dbname') or config.get('database')

        try:
            connection = psycopg2.connect(**config)
            return connection
        except psycopg2.OperationalError as e:
            if 'does not exist' in str(e) or 'FATAL' in str(e):
                logger.warning("Database '%s' does not exist, creating...", dbname)

                postgres_config = config.copy()
                postgres_config['dbname'] = 'postgres'

                try:
                    with psycopg2.connect(**postgres_config) as temp_conn:
                        temp_conn.autocommit = True
                        with temp_conn.cursor() as cursor:
                            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
                            if not cursor.fetchone():
                                cursor.execute(f'CREATE DATABASE "{dbname}"')
                                logger.info("Database '%s' created", dbname)
                            else:
                                logger.info("Database '%s' already exists", dbname)

                    connection = psycopg2.connect(**config)
                    return connection

                except Exception as create_error:
                    logger.error("Failed to create database '%s': %s", dbname, create_error)
                    raise create_error
            else:
                raise e

    except Exception as e:
        logger.error("PostgreSQL connection failed: %s", e)
        raise
    
def delete_entities_from_pg(ids: list[str], dataset: str):
        """Delete entities by node_id."""
        if not ids:
            return
        entities_table = f"{dataset}_entities"
        conn = get_postgres_connection()
        try:
            with conn.cursor() as cur:
                sql = f"DELETE FROM public.{entities_table} WHERE node_id = ANY(%s)"
                cur.execute(sql, (ids,))
            conn.commit()
            logger.info("Deleted %s entity rows from PG", len(ids))
        except Exception as e:
            logger.error("Failed to delete PG entities: %s", e)
            conn.rollback()
            raise
        finally:
            conn.close()
            
def insert_entities_to_pg(records: list[dict[str, Any]], dataset: str):
        """Batch-insert entity records into PostgreSQL."""
        entities_table = f"{dataset}_entities"
        insert_entities_to_pg_table(records, entities_table)


def insert_entities_to_pg_table(records: list[dict[str, Any]], table_name: str) -> None:
        """Batch-insert entity records into an explicit PostgreSQL table."""
        if not records:
            return
        conn = get_postgres_connection()
        try:
            with conn.cursor() as cur:
                sql = f"""
                    INSERT INTO public.{table_name}
                    (node_name, type, sub_type, description, node_id)
                    VALUES %s
                """
                batch_data = []
                for rec in records:
                    batch_data.append(
                        (
                            clean_str(rec.get("NodeName", ""))[:500],
                            "attr",
                            clean_str(rec.get("SubType", ""))[:100],
                            clean_str(rec.get("Description", "")),
                            clean_str(rec.get("Id", ""))[:128],
                        )
                    )
                execute_values(cur, sql, batch_data, template=None, page_size=1000)
            conn.commit()
            logger.info("Inserted %s entity rows to PG table %s", len(records), table_name)
        except Exception as e:
            logger.error("Failed to insert PG entities into %s: %s", table_name, e)
            conn.rollback()
            raise
        finally:
            conn.close()
def create_chunk_table(chunk_table, postgres_config):
    """Create the chunk table and indexes if they do not exist."""
    idx_name = f"idx_{chunk_table}_chunk_id"
    tsv_idx_name = f"idx_{chunk_table}_tsv"
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS public.{chunk_table} (
        id SERIAL PRIMARY KEY,
        chunk_id VARCHAR(128),
        content_text TEXT,
        content_tsv tsvector,
        source_id TEXT,
        source_chunk TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS {idx_name} ON public.{chunk_table}(chunk_id);
    CREATE INDEX IF NOT EXISTS {tsv_idx_name} ON public.{chunk_table} USING GIN(content_tsv);
    """
    
    conn = psycopg2.connect(**postgres_config)
    try:
        with conn.cursor() as cursor:
            cursor.execute(create_table_sql)
            conn.commit()
            logger.info("Chunk table '%s' ensured", chunk_table)
    except Exception as e:
        logger.error("Failed to create chunk table: %s", e)
        raise
    finally:
        conn.close()



    
def batch_insert_chunks_pg(
    chunks_data: list[dict[str, Any]],
    postgres_config: dict[str, Any],
    chunk_table: str,
    postgres_batch_size,
):
    """Batch-insert chunk records into the given PostgreSQL chunk table."""
    if not chunks_data:
        return

    cfg = postgres_config
    table = chunk_table

    conn = psycopg2.connect(**cfg)
    try:
        with conn.cursor() as cursor:
            insert_sql = f"""
            INSERT INTO public.{table}
            (chunk_id, content_text, content_tsv, source_id, source_chunk)
            VALUES %s
            """

            batch_data = []
            for chunk in chunks_data:
                chunk_id = clean_str(chunk.get("chunk_id", ""), 128)
                content_text = clean_str(chunk.get("text", ""), 65000)
                tokenized = tokenize_for_tsvector(content_text)
                source = clean_str(chunk.get("source", ""), 1000)
                source_chunk = clean_str(chunk.get("source_chunk", json.dumps([chunk.get("chunk_id")])), 1000)
                batch_data.append((chunk_id, content_text, tokenized, source, source_chunk))

            execute_values(
                cursor, insert_sql, batch_data,
                template="(%s, %s, to_tsvector('simple', %s), %s, %s)",
                page_size=postgres_batch_size,
            )
            conn.commit()
            logger.info("Inserted %s chunks into %s", len(chunks_data), table)

    except Exception as e:
        logger.error("Chunk batch insert failed: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()

def insert_relations_to_pg(records: list[dict[str, Any]], dataset: str) -> None:
    """Batch-insert relation records into PostgreSQL."""
    relations_table = f"{dataset}_relations"
    insert_relations_to_pg_table(records, relations_table)


def insert_relations_to_pg_table(records: list[dict[str, Any]], table_name: str) -> None:
    """Batch-insert relation records into an explicit PostgreSQL table."""
    if not records:
        return

    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"""
            INSERT INTO public.{table_name}
            (node1, node2, type, semantics, description, node_id)
            VALUES %s
            """
            batch_data = []
            for rel in records:
                batch_data.append(
                    (
                        clean_str(rel.get("Node1", ""))[:500],
                        clean_str(rel.get("Node2", ""))[:500],
                        "triple",
                        clean_str(rel.get("Relation", ""))[:100],
                        clean_str(rel.get("Description", "")),
                        clean_str(rel.get("Id", ""))[:128],
                    )
                )
            execute_values(cur, sql, batch_data, template=None, page_size=1000)
        conn.commit()
        logger.info("Inserted %d relation rows to PG table %s", len(records), table_name)
    except Exception as e:
        logger.error("Failed to insert PG relations into %s: %s", table_name, e)
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_relations_from_pg(ids: list[str], dataset: str) -> None:
    """Delete relations by node_id."""
    if not ids:
        return
    relations_table = f"{dataset}_relations"
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"DELETE FROM public.{relations_table} WHERE node_id = ANY(%s)"
            cur.execute(sql, (ids,))
        conn.commit()
        logger.info("Deleted %d relation rows from PG", len(ids))
    except Exception as e:
        logger.error("Failed to delete PG relations: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_cluster_chunk_relations(relations: list[dict[str, Any]], dataset: str) -> None:
    """Batch-insert cluster-chunk relations (ON CONFLICT DO NOTHING)."""
    table_name = f"{dataset}_cluster_chunk_relation"
    insert_cluster_chunk_relations_table(relations, table_name)


def insert_cluster_chunk_relations_table(relations: list[dict[str, Any]], table_name: str) -> None:
    """Batch-insert cluster-chunk relations into an explicit PostgreSQL table."""
    if not relations:
        return

    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"""
            INSERT INTO public.{table_name}
            (cluster_id, chunk_id, url, type, meta)
            VALUES %s
            ON CONFLICT DO NOTHING
            """
            batch_data = []
            for rel in relations:
                meta = rel.get("meta", {})
                batch_data.append(
                    (
                        clean_str(rel.get("cluster_id", ""), 100),
                        clean_str(rel.get("chunk_id", ""), 125),
                        clean_str(rel.get("url", ""), 65535),
                        clean_str(rel.get("type", ""), 32),
                        json.dumps(meta, ensure_ascii=False) if meta else None,
                    )
                )
            execute_values(cur, sql, batch_data, template=None, page_size=1000)
        conn.commit()
        logger.info("Inserted %d cluster-chunk relations to PG table %s", len(relations), table_name)
    except Exception as e:
        logger.error("Failed to insert cluster-chunk relations into %s: %s", table_name, e)
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_cluster_chunk_relations_by_cluster_ids(cluster_ids: list[str], dataset: str) -> None:
    """Delete cluster-chunk relations by cluster_id."""
    if not cluster_ids:
        return
    
    table_name = f"{dataset}_cluster_chunk_relation"
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"DELETE FROM public.{table_name} WHERE cluster_id = ANY(%s)"
            cur.execute(sql, (cluster_ids,))
        conn.commit()
        logger.info("Deleted cluster-chunk relations for %d cluster_ids", len(cluster_ids))
    except Exception as e:
        logger.error("Failed to delete cluster-chunk relations: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_chunks_by_source_ids(source_ids: list[str], dataset: str) -> int:
    """Delete chunks by source_id, return the number of deleted rows."""
    if not source_ids:
        return 0
    
    chunk_table = f"{dataset}_chunks"
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"DELETE FROM public.{chunk_table} WHERE source_id = ANY(%s)"
            cur.execute(sql, (source_ids,))
            deleted_count = cur.rowcount
        conn.commit()
        logger.info("Deleted %d chunks from %s for %d sources", deleted_count, chunk_table, len(source_ids))
        return deleted_count
    except Exception as e:
        logger.error("Failed to delete chunks by source_ids: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_cluster_chunk_relations_by_urls(urls: list[str], dataset: str) -> int:
    """Delete cluster-chunk relations by url, return the number of deleted rows."""
    if not urls:
        return 0
    
    table_name = f"{dataset}_cluster_chunk_relation"
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"DELETE FROM public.{table_name} WHERE url = ANY(%s)"
            cur.execute(sql, (urls,))
            deleted_count = cur.rowcount
        conn.commit()
        logger.info("Deleted %d cluster-chunk relations from %s for %d urls", deleted_count, table_name, len(urls))
        return deleted_count
    except Exception as e:
        logger.error("Failed to delete cluster-chunk relations by urls: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def drop_dataset_tables(dataset: str) -> None:
    """Drop all PostgreSQL tables for a dataset."""
    tables_to_drop = [
        f"{dataset}_chunks",
        f"{dataset}_entities", 
        f"{dataset}_relations",
        f"{dataset}_cluster_chunk_relation",
    ]
    
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            for table_name in tables_to_drop:
                try:
                    cur.execute(f'DROP TABLE IF EXISTS public."{table_name}" CASCADE')
                    logger.info("Dropped table: %s", table_name)
                except Exception as e:
                    logger.warning("Error dropping table %s: %s", table_name, e)
            conn.commit()
    except Exception as e:
        logger.error("Failed to drop dataset tables: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def get_chunk_source_mapping(chunk_ids: list[str], chunk_table: str) -> dict[str, str]:
    """Return {chunk_id: source_id} for the given chunk IDs."""
    if not chunk_ids:
        return {}
    
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"SELECT chunk_id, source_id FROM public.{chunk_table} WHERE chunk_id = ANY(%s)"
            cur.execute(sql, (chunk_ids,))
            rows = cur.fetchall()
            return {row[0]: row[1] for row in rows if row[1]}
    except Exception as e:
        logger.error("Failed to query chunk source mapping: %s", e)
        return {}
    finally:
        conn.close()


def query_cluster_chunk_relations_by_urls(urls: list[str], dataset: str) -> list[dict[str, Any]]:
    """Query cluster-chunk relations by url."""
    if not urls:
        return []
    
    table_name = f"{dataset}_cluster_chunk_relation"
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = f"""
                SELECT cluster_id, chunk_id, url, type 
                FROM public.{table_name} 
                WHERE url = ANY(%s)
                ORDER BY url, cluster_id, chunk_id
            """
            cur.execute(sql, (urls,))
            rows = cur.fetchall()
            
            results = []
            for row in rows:
                results.append({
                    "cluster_id": row[0],
                    "chunk_id": row[1],
                    "url": row[2],
                    "type": row[3]
                })
            
            logger.info("Queried %d cluster-chunk relations from %s for %d urls", len(results), table_name, len(urls))
            return results
    except Exception as e:
        logger.error("Failed to query cluster-chunk relations by urls: %s", e)
        return []
    finally:
        conn.close()


def get_cluster_chunk_mapping(cluster_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    """Return {cluster_id: [{chunk_id, url}, ...]} for the given cluster IDs."""
    if not cluster_ids:
        return {}
    
    conn = get_postgres_connection()
    try:
        with conn.cursor() as cur:
            sql = """
            SELECT cluster_id, chunk_id, url 
            FROM public.cluster_chunk_relation 
            WHERE cluster_id = ANY(%s)
            """
            cur.execute(sql, (cluster_ids,))
            rows = cur.fetchall()
            
            mapping = {}
            for row in rows:
                cluster_id, chunk_id, url = row
                if cluster_id not in mapping:
                    mapping[cluster_id] = []
                mapping[cluster_id].append({
                    "chunk_id": chunk_id,
                    "url": url
                })
            
            return mapping
    except Exception as e:
        logger.error("Failed to query cluster-chunk mapping: %s", e)
        return {}
    finally:
        conn.close()



def create_cluster_chunk_relation_table(dataset: str):
    """Create the cluster_chunk_relation table and indexes if they do not exist."""
    table_name = f"{dataset}_cluster_chunk_relation"
    create_cluster_chunk_relation_table_name(table_name)


def create_cluster_chunk_relation_table_name(table_name: str) -> None:
    """Create an explicit cluster_chunk_relation table and indexes if needed."""
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS public.{table_name} (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        meta JSONB,
        cluster_id VARCHAR(100) NOT NULL,
        chunk_id VARCHAR(125) NOT NULL,
        url TEXT NOT NULL,
        type VARCHAR(32)
    );
    CREATE INDEX IF NOT EXISTS idx_{table_name}_cluster_id ON public.{table_name}(cluster_id);
    CREATE INDEX IF NOT EXISTS idx_{table_name}_chunk_id ON public.{table_name}(chunk_id);
    """
    
    connection = get_postgres_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(create_table_sql)
            connection.commit()
            logger.info("Cluster-chunk relation table '%s' ensured", table_name)
    except Exception as e:
        logger.error("Failed to create cluster-chunk relation table: %s", e)
        raise
    finally:
        connection.close()


def create_graph_tables(dataset: str):
    """Create entities, relations, and cluster-chunk-relation tables."""
    entities_table = f"{dataset}_entities"
    relations_table = f"{dataset}_relations"
    create_graph_tables_named(
        entities_table=entities_table,
        relations_table=relations_table,
        cluster_chunk_relation_table=f"{dataset}_cluster_chunk_relation",
    )


def create_graph_tables_named(
    entities_table: str,
    relations_table: str,
    cluster_chunk_relation_table: str,
) -> None:
    """Create entities, relations, and cluster-chunk-relation tables by explicit names."""
    create_entities_sql = f"""
    CREATE TABLE IF NOT EXISTS public.{entities_table} (
        id SERIAL PRIMARY KEY,
        node_name VARCHAR(500),
        type VARCHAR(100),
        sub_type VARCHAR(100),
        description TEXT,
        node_id VARCHAR(128),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_{entities_table}_node_id ON public.{entities_table}(node_id);
    """

    create_relations_sql = f"""
    CREATE TABLE IF NOT EXISTS public.{relations_table} (
        id SERIAL PRIMARY KEY,
        node1 VARCHAR(500),
        node2 VARCHAR(500),
        type VARCHAR(100),
        semantics VARCHAR(100),
        description TEXT,
        node_id VARCHAR(128),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_{relations_table}_node_id ON public.{relations_table}(node_id);
    """

    connection = get_postgres_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(create_entities_sql)
            cursor.execute(create_relations_sql)
            connection.commit()
            logger.info("Graph tables ensured: %s, %s", entities_table, relations_table)

        create_cluster_chunk_relation_table_name(cluster_chunk_relation_table)
    except Exception as e:
        logger.error("Failed to create graph tables: %s", e)
        raise
    finally:
        connection.close()


def read_local_chunks(data_dir: str) -> list[dict[str, Any]]:
    """Read all chunk JSONL files from the data directory."""
    paths = get_data_paths(data_dir)
    chunk_path = Path(paths["chunks_dir"])

    files: list[Path] = []
    if chunk_path.is_dir():
        files = sorted(chunk_path.glob("*.jsonl"))
    elif chunk_path.is_file():
        files = [chunk_path]

    if not files:
        logger.warning("No chunk JSONL files found: %s", chunk_path)
        return []

    data: list[dict[str, Any]] = []
    for jsonl_file in files:
        with jsonl_file.open(encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    obj = json.loads(line_stripped)
                    chunk_id = str(obj.get("chunk_id", "") or obj.get("ChunkId", "") or obj.get("chunkid", ""))
                    text = str(obj.get("Text", "") or obj.get("text", ""))
                    if not chunk_id or not text:
                        continue
                    data.append({"chunk_id": chunk_id, "text": text})
                except json.JSONDecodeError as e:
                    logger.warning("[%s] line %s parse error: %s", jsonl_file.name, line_num, e)
                    continue

    logger.info("Read %s local chunks from %s files", len(data), len(files))
    return data




def read_local_nodes(data_dir: str) -> list[dict[str, Any]]:
    """Read all node JSONL files from the data directory."""
    paths = get_data_paths(data_dir)
    nodes_dir = paths["nodes_dir"]
    if not os.path.exists(nodes_dir):
        logger.warning("Nodes directory not found: %s", nodes_dir)
        return []

    nodes_path = Path(nodes_dir)
    jsonl_files = sorted(list(nodes_path.glob("*.jsonl")))
    if not jsonl_files:
        logger.warning("No JSONL files found in: %s", nodes_dir)
        return []

    data: list[dict[str, Any]] = []
    for jsonl_file in jsonl_files:
        file_count = 0
        with open(jsonl_file, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    obj = json.loads(line_stripped)
                    data.append(obj)
                    file_count += 1
                except json.JSONDecodeError as e:
                    logger.warning("Parse error at %s:%s: %s", jsonl_file, line_num, e)
                    continue
        logger.info("Loaded %s nodes from %s", file_count, jsonl_file)

    logger.info("Read %s local nodes from %s files", len(data), len(jsonl_files))
    return data


def read_local_relations(data_dir: str) -> list[dict[str, Any]]:
    """Read all relation JSONL files from the data directory."""
    paths = get_data_paths(data_dir)
    relations_dir = paths["relations_dir"]
    if not os.path.exists(relations_dir):
        logger.warning("Relations directory not found: %s", relations_dir)
        return []

    relations_path = Path(relations_dir)
    jsonl_files = sorted(list(relations_path.glob("*.jsonl")))
    if not jsonl_files:
        logger.warning("No JSONL files found in: %s", relations_dir)
        return []

    data: list[dict[str, Any]] = []
    for jsonl_file in jsonl_files:
        file_count = 0
        with open(jsonl_file, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    obj = json.loads(line_stripped)
                    data.append(obj)
                    file_count += 1
                except json.JSONDecodeError as e:
                    logger.warning("Parse error at %s:%s: %s", jsonl_file, line_num, e)
                    continue
        logger.info("Loaded %s relations from %s", file_count, jsonl_file)

    logger.info("Read %s local relations from %s files", len(data), len(jsonl_files))
    return data



