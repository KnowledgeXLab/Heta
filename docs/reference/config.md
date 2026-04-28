# Configuration Reference

Two files control Heta's behaviour:

- **`config.yaml`** (project root) — API keys, database connections, and model selections per module. Copy from `config.example.yaml` and fill in your credentials.
- **`src/hetadb/config/db_config.yaml`** — Processing pipeline tuning (batch sizes, worker counts, chunking). Rarely needs to change.

**Required vs optional** — Keys marked **Required** must be set before starting. Everything else has a working default.

---

## config.yaml

### `providers`

Credential blocks shared across modules via YAML anchors. Define the providers you use; unused ones can be left empty or removed.

| Key | Required | Description |
|-----|----------|-------------|
| `providers.dashscope.api_key` | Required by selected template | Alibaba Cloud DashScope API key |
| `providers.dashscope.base_url` | — | DashScope OpenAI-compatible endpoint (pre-filled) |
| `providers.siliconflow.api_key` | Required | SiliconFlow API key (used for embeddings) |
| `providers.siliconflow.base_url` | — | SiliconFlow endpoint (pre-filled) |
| `providers.openai.api_key` | Required by selected template | OpenAI API key |
| `providers.openai.base_url` | — | OpenAI endpoint (pre-filled) |
| `providers.gemini.api_key` | Required by selected template | Google Gemini API key |
| `providers.gemini.base_url` | — | Gemini OpenAI-compatible endpoint (pre-filled) |

---

### `persistence`

Connection strings for the three backing stores. When using Docker Compose the defaults are pre-configured and require no changes.

#### PostgreSQL

| Key | Default | Description |
|-----|---------|-------------|
| `persistence.postgresql.host` | `postgres` | Host name (use `localhost` outside Docker) |
| `persistence.postgresql.port` | `5432` | Port |
| `persistence.postgresql.user` | `postgres` | Username |
| `persistence.postgresql.password` | `postgres` | Password |
| `persistence.postgresql.database` | `postgres` | Database name |

#### Milvus

| Key | Default | Description |
|-----|---------|-------------|
| `persistence.milvus.host` | `milvus` | Host name |
| `persistence.milvus.port` | `19530` | gRPC port |
| `persistence.milvus.url` | `http://milvus:19530` | Full URI — overrides host/port when set |

#### Neo4j

Used by HetaMem MemoryVG only.

| Key | Default | Description |
|-----|---------|-------------|
| `persistence.neo4j.url` | `bolt://neo4j:7687` | Bolt connection URL |
| `persistence.neo4j.username` | `neo4j` | Username |
| `persistence.neo4j.password` | `heta_password` | Password |
| `persistence.neo4j.database` | `neo4j` | Database name |

---

### `hetadb`

#### `hetadb.llm`

LLM for graph entity/relation extraction and answer synthesis.

| Key | Default | Description |
|-----|---------|-------------|
| `hetadb.llm.model` | `qwen3-32b` | Model name |
| `hetadb.llm.max_concurrent_requests` | `10` | Max parallel LLM calls |
| `hetadb.llm.max_retries` | `3` | Retry attempts on failure |
| `hetadb.llm.timeout` | `120` | Request timeout in seconds |

#### `hetadb.vlm`

Vision-language model for PDF page and image understanding.

| Key | Default | Description |
|-----|---------|-------------|
| `hetadb.vlm.model` | `qwen2.5-vl-72b-instruct` | VLM model name |
| `hetadb.vlm.max_concurrent_requests` | `10` | Max parallel VLM calls |
| `hetadb.vlm.max_retries` | `5` | Retry attempts on failure |
| `hetadb.vlm.timeout` | `120` | Request timeout in seconds |

#### `hetadb.embedding_api`

Embedding model for chunk and node vector indexing.

| Key | Default | Description |
|-----|---------|-------------|
| `hetadb.embedding_api.model` | `BAAI/bge-m3` | Embedding model name |
| `hetadb.embedding_api.dim` | `1024` | Vector dimension — must match Milvus collection schema |
| `hetadb.embedding_api.timeout` | `30` | Request timeout in seconds |
| `hetadb.embedding_api.batch_size` | `2000` | Records per embedding queue batch |
| `hetadb.embedding_api.num_threads` | `8` | Parallel worker threads for embedding |
| `hetadb.embedding_api.max_retries` | `5` | Retry attempts on failure |
| `hetadb.embedding_api.retry_delay` | `2` | Initial retry delay in seconds (doubles each retry) |

#### `hetadb.milvus`

| Key | Default | Description |
|-----|---------|-------------|
| `hetadb.milvus.db_name` | `hetadb` | Milvus database name for HetaDB collections |
| `hetadb.milvus.sentence_mode` | `false` | Use sentence-level chunking instead of paragraph-level |

#### `hetadb.query_defaults`

Default retrieval parameters when the caller does not specify them.

| Key | Default | Description |
|-----|---------|-------------|
| `hetadb.query_defaults.top_k` | `10` | Vector candidate pool size per query |
| `hetadb.query_defaults.threshold` | `0.0` | Minimum similarity score to include a result |
| `hetadb.query_defaults.similarity_weight` | `1.5` | Weight multiplier for vector similarity scores |
| `hetadb.query_defaults.occur_weight` | `1.0` | Weight multiplier for KG occurrence counts |
| `hetadb.query_defaults.reranker_url` | *(unset)* | Base URL of a self-hosted cross-encoder reranker service. Required to enable full `rerank` mode; if unset, `rerank` falls back to RRF ordering without cross-encoder scoring. Expected API: `POST /rerank` with body `{"pairs": [["query", "doc"], ...]}` returning `{"scores": [...]}`. Recommended model: Qwen3-Reranker. |

#### `hetadb.search_params`

| Key | Default | Description |
|-----|---------|-------------|
| `hetadb.search_params.ef_multiplier` | `10` | HNSW ef = top_k × ef_multiplier; higher = more accurate but slower |

---

### `hetamem`

#### `hetamem.memorykb`

MemoryKB uses LightRAG (NanoVectorDB + NetworkX) — lightweight and low-latency.

| Key | Default | Domestic alternative |
|-----|---------|---------------------|
| `hetamem.memorykb.llm.model` | `gpt-4o-mini-2024-07-18` | `qwen-plus` via DashScope |
| `hetamem.memorykb.embedding.model` | `text-embedding-3-small` | `text-embedding-v4` via DashScope |
| `hetamem.memorykb.embedding.dim` | `1536` | `1536` |

#### `hetamem.memoryvg`

MemoryVG uses Milvus + Neo4j for structured fact storage across conversations.

| Key | Default | Domestic alternative |
|-----|---------|---------------------|
| `hetamem.memoryvg.llm.config.model` | `qwen3-32b` | same |
| `hetamem.memoryvg.embedder.config.model` | `text-embedding-3-large` | `BAAI/bge-m3` via SiliconFlow |
| `hetamem.memoryvg.embedder.config.embedding_dims` | `1024` | `1024` |
| `hetamem.memoryvg.vector_store.config.collection_name` | `memoryvg` | Milvus collection name |
| `hetamem.memoryvg.vector_store.config.db_name` | `hetamem` | Milvus database name |

!!! note
    `hetamem.memoryvg.graph_store` inherits Neo4j credentials from `persistence.neo4j` — no separate config needed.

---

### `hetagen`

| Key | Default | Domestic alternative |
|-----|---------|---------------------|
| `hetagen.llm.model` | `gemini-3-flash-preview` | `qwen3-32b` via DashScope |
| `hetagen.llm.max_concurrent_requests` | `10` | — |
| `hetagen.llm.timeout` | `120` | — |
| `hetagen.vlm.model` | `Qwen/Qwen3-VL-32B-Instruct` | same |
| `hetagen.embedding_api.model` | `BAAI/bge-m3` | same |
| `hetagen.embedding_api.dim` | `1024` | — |

---

### `hetawiki`

HetaWiki uses a small configuration block for ingest limits and the LLM used by
default ingest, merge ingest, query, and lint.

#### `hetawiki.workspace`

| Key | Current value | Description |
|-----|---------------|-------------|
| `hetawiki.workspace` | `"workspace"` | Workspace root setting in `config.yaml`. Note: the current HetaWiki path layer still uses the fixed local path `workspace/hetawiki/`, so changing this key does not yet relocate the wiki workspace. |

#### `hetawiki.ingest`

| Key | Current value | Description |
|-----|---------------|-------------|
| `hetawiki.ingest.max_workers` | `4` | Max concurrent ingest worker threads. The query router currently reuses the same thread-pool size. |
| `hetawiki.ingest.max_input_chars` | `80000` | Maximum parsed text length accepted by `default ingest`. Longer documents are rejected instead of being silently truncated or auto-split. |
| `hetawiki.ingest.parse_timeout` | `300` | Parser timeout in seconds for MinerU-based document parsing. Large PDFs or OCR-heavy files may need several minutes. |

#### `hetawiki.llm`

| Key | Current value | Description |
|-----|---------------|-------------|
| `hetawiki.llm.base_url` | inherited from `*dashscope` | OpenAI-compatible base URL used by HetaWiki LLM calls. Required. |
| `hetawiki.llm.api_key` | inherited from `*dashscope` | API key for the configured provider. Required. |
| `hetawiki.llm.model` | `"qwen3.5-flash"` | Model currently used for default ingest, merge ingest, query, and lint. |
| `hetawiki.llm.timeout` | `300` | Request timeout in seconds. Merge and long-document calls may take substantially longer than ordinary chat requests. |
| `hetawiki.llm.max_retries` | `2` | Retry attempts for failed HetaWiki LLM requests. |

---

## db_config.yaml

Located at `src/hetadb/config/db_config.yaml`. Controls document processing pipeline throughput. The defaults work well for most hardware; adjust when scaling to large datasets.

### Top-level

| Key | Default | Description |
|-----|---------|-------------|
| `postgres_batch_size` | `500` | Row batch size for bulk PostgreSQL inserts |
| `parse_max_workers` | `2` | Max concurrent document parse tasks |

### `parameter.chunk_config`

| Key | Default | Description |
|-----|---------|-------------|
| `chunk_size` | `1024` | Target chunk size in tokens |
| `overlap` | `50` | Token overlap between consecutive chunks |
| `max_workers` | `16` | Thread pool size for parallel chunking |

### `parameter.graph_config`

| Key | Default | Description |
|-----|---------|-------------|
| `batch_size` | `2000` | Chunks per graph extraction batch |
| `max_workers` | `200` | Max concurrent LLM calls for entity/relation extraction |

### `parameter.graph_merge_config`

| Key | Default | Description |
|-----|---------|-------------|
| `parallel_batches` | `16` | Merge batches to process in parallel |
| `batch_size` | `500` | Entities per merge batch |

### `parameter.vector_config`

| Key | Default | Description |
|-----|---------|-------------|
| `batch_size` | `2000` | Chunks per embedding API call batch |
| `num_threads` | `8` | Threads for concurrent Milvus upsert operations |
