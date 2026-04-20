# API Reference

Complete REST API reference for all Heta modules.

**Base URL:** `http://<host>:8000`
**Content-Type:** `application/json` unless noted otherwise

---

## Table of Contents

- [System](#system)
- [HetaWiki — Ingest](#hetawiki--ingest)
- [HetaWiki — Query](#hetawiki--query)
- [HetaWiki — Lint](#hetawiki--lint)
- [HetaWiki — Read](#hetawiki--read)
- [HetaWiki — Tasks](#hetawiki--tasks)
- [HetaDB — Chat](#hetadb--chat)
- [HetaDB — Datasets](#hetadb--datasets)
- [HetaDB — Knowledge Bases](#hetadb--knowledge-bases)
- [HetaDB — Processing Tasks](#hetadb--processing-tasks)
- [HetaDB — Config](#hetadb--config)
- [HetaDB — Schemas](#hetadb--schemas)
- [HetaGen — Table Generation](#hetagen--table-generation)
- [HetaGen — Tag Tree](#hetagen--tag-tree)
- [HetaMem — MemoryKB](#hetamem--memorykb)
- [HetaMem — MemoryVG](#hetamem--memoryvg)

---

## Endpoint Index

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info |
| GET | `/health` | Health check |
| **HetaWiki — Ingest** | | |
| POST | `/api/v1/hetawiki/ingest` | Upload a file and start wiki ingest |
| **HetaWiki — Query** | | |
| POST | `/api/v1/hetawiki/query` | Ask a question about the wiki |
| **HetaWiki — Lint** | | |
| POST | `/api/v1/hetawiki/lint` | Update lint scheduler config |
| **HetaWiki — Read** | | |
| GET | `/api/v1/hetawiki/index` | Read the wiki index |
| GET | `/api/v1/hetawiki/pages/{filename}` | Read a wiki page |
| GET | `/api/v1/hetawiki/graph` | Read wiki graph nodes and edges |
| **HetaWiki — Tasks** | | |
| GET | `/api/v1/hetawiki/tasks/{task_id}` | Get ingest/query task status |
| **HetaDB — Chat** | | |
| POST | `/api/v1/hetadb/chat` | Query a knowledge base |
| **HetaDB — Datasets** | | |
| GET | `/api/v1/hetadb/files/raw-files/datasets` | List datasets |
| POST | `/api/v1/hetadb/files/raw-files/datasets` | Create dataset |
| GET | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/files` | List files in dataset |
| POST | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/files` | Upload files (simple) |
| DELETE | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/files/{filename}` | Delete file |
| DELETE | `/api/v1/hetadb/files/raw-files/datasets/{dataset}` | Delete dataset |
| POST | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/init` | Init chunked upload |
| POST | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/{upload_id}/chunk` | Upload chunk |
| POST | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/{upload_id}/complete` | Complete upload |
| DELETE | `/api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/{upload_id}` | Abort upload |
| **HetaDB — Knowledge Bases** | | |
| GET | `/api/v1/hetadb/files/knowledge-bases` | List knowledge bases |
| POST | `/api/v1/hetadb/files/knowledge-bases` | Create knowledge base |
| GET | `/api/v1/hetadb/files/knowledge-bases/{kb_name}` | Get KB detail |
| GET | `/api/v1/hetadb/files/knowledge-bases/{kb_name}/overview` | Get KB overview |
| DELETE | `/api/v1/hetadb/files/knowledge-bases/{kb_name}` | Delete KB (async) |
| DELETE | `/api/v1/hetadb/files/knowledge-bases/{kb_name}/datasets/{dataset_name}` | Remove dataset from KB |
| POST | `/api/v1/hetadb/files/knowledge-bases/{kb_name}/parse` | Trigger document parsing |
| **HetaDB — Processing Tasks** | | |
| GET | `/api/v1/hetadb/files/processing/tasks` | List tasks |
| GET | `/api/v1/hetadb/files/processing/tasks/{task_id}` | Get task |
| POST | `/api/v1/hetadb/files/processing/tasks/{task_id}/cancel` | Cancel task |
| GET | `/api/v1/hetadb/files/processing/config` | Get processing config |
| **HetaDB — Config** | | |
| GET | `/api/v1/hetadb/config` | Get full config |
| GET | `/api/v1/hetadb/config/{section}` | Get config section |
| POST | `/api/v1/hetadb/config/reload` | Reload config from disk |
| **HetaDB — Schemas** | | |
| POST | `/api/v1/hetadb/schemas` | Create entity schema |
| GET | `/api/v1/hetadb/schemas` | List schemas |
| GET | `/api/v1/hetadb/schemas/{name}` | Get schema detail |
| DELETE | `/api/v1/hetadb/schemas/{name}` | Delete schema |
| **HetaGen — Table Generation** | | |
| POST | `/api/v1/hetagen/pipeline/submit` | Submit table generation task |
| GET | `/api/v1/hetagen/pipeline/status/{task_id}` | Get task status |
| WS | `/api/v1/hetagen/pipeline/stream` | Stream execution (WebSocket) |
| **HetaGen — Tag Tree** | | |
| POST | `/api/v1/hetagen/tag-tree/generate` | Generate knowledge tree |
| POST | `/api/v1/hetagen/tag-tree/submit` | Submit Excel tree task (legacy) |
| GET | `/api/v1/hetagen/tag-tree/status/{task_id}` | Get task status |
| **HetaMem — MemoryKB** | | |
| POST | `/api/v1/hetamem/kb/insert` | Insert knowledge (async) |
| POST | `/api/v1/hetamem/kb/query` | Query knowledge graph |
| **HetaMem — MemoryVG** | | |
| POST | `/api/v1/hetamem/vg/add` | Extract and store memories |
| POST | `/api/v1/hetamem/vg/search` | Search memories |
| GET | `/api/v1/hetamem/vg` | List memories |
| GET | `/api/v1/hetamem/vg/{memory_id}` | Get memory |
| GET | `/api/v1/hetamem/vg/{memory_id}/history` | Memory modification history |
| PUT | `/api/v1/hetamem/vg/{memory_id}` | Update memory |
| DELETE | `/api/v1/hetamem/vg/{memory_id}` | Delete memory |
| DELETE | `/api/v1/hetamem/vg` | Delete all memories (scoped) |

---

## Common Conventions

**Names** — Dataset and KB names must match `^[A-Za-z0-9_\-]+$`, max 64 characters.

**Task states** — Heta asynchronous endpoints typically move through `pending` → `running` → `completed` | `failed`. Some modules may expose additional task states or cancellation endpoints; check the module-specific endpoint docs before relying on them.

**Standard response envelope** (HetaDB endpoints):
```json
{ "success": true, "message": "...", "data": { ... } }
```

**HTTP errors** — HetaDB uses real HTTP status codes (404, 409, 500). HetaMem uses HTTP status codes too. HetaDB Chat returns HTTP 200 for all responses; check the `success` and `code` fields.

---

## System

### GET /

Returns service name and version.

**Response**
```json
{ "service": "Heta API", "version": "0.1.0" }
```

---

### GET /health

**Response**
```json
{ "status": "ok" }
```

---

## HetaWiki — Ingest

### POST /api/v1/hetawiki/ingest

Upload one file and start an asynchronous wiki ingest task.

Use `multipart/form-data`.

**Form fields**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `file` | file | ✓ | Source document to ingest |
| `merge` | boolean |  | `false` = add as new page; `true` = integrate with existing wiki |

**Response**

```json
{
  "task_id": "abc123",
  "status": "queued",
  "filename": "2026-04-17_194500_deepseek-r1.pdf"
}
```

---

## HetaWiki — Query

### POST /api/v1/hetawiki/query

Submit a question about the current wiki. Query runs asynchronously and returns
a task ID.

**Request body**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `question` | string | ✓ | Natural-language question about the wiki |

**Response**

```json
{
  "task_id": "abc123",
  "status": "queued"
}
```

The immediate response only returns `task_id` and `status`.

To get the final answer, poll `GET /api/v1/hetawiki/tasks/{task_id}`. When the task completes, `result` includes:

- `answer`
- `sources`
- `archived`

---

## HetaWiki — Lint

### POST /api/v1/hetawiki/lint

Update the HetaWiki lint scheduler configuration.

**Request body**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `enabled` | boolean\|null |  | Enable or disable scheduled lint |
| `interval_hours` | integer\|null |  | Hours between lint runs |

**Response**

```json
{
  "interval_hours": 24,
  "enabled": true,
  "next_run": "2026-04-18T02:00:00"
}
```

---

## HetaWiki — Read

### GET /api/v1/hetawiki/index

Read the current wiki index.

**Response**

```json
{
  "content": "# Wiki Index\n..."
}
```

### GET /api/v1/hetawiki/pages/{filename}

Read one wiki page by filename.

**Response**

```json
{
  "path": "pages/DeepSeek-R1.md",
  "content": "---\ntitle: DeepSeek-R1\n..."
}
```

### GET /api/v1/hetawiki/graph

Return wiki pages as graph nodes and `[[links]]` as edges.

**Response**

```json
{
  "nodes": [
    { "id": "pages/DeepSeek-R1", "title": "DeepSeek-R1", "category": null }
  ],
  "edges": [
    { "source": "pages/DeepSeek-R1", "target": "pages/GRPO" }
  ]
}
```

---

## HetaWiki — Tasks

### GET /api/v1/hetawiki/tasks/{task_id}

Read the current state of an ingest or query task.

**Response**

```json
{
  "task_id": "abc123",
  "status": "completed",
  "message": "done",
  "error": null,
  "result": {}
}
```

---

## HetaDB — Chat

### POST /api/v1/hetadb/chat

Query a knowledge base with LLM-synthesised answer and source citations.

The server auto-resolves the `process_mode` from each dataset's metadata and validates that `query_mode` is compatible. All responses use HTTP 200; check `success` and `code`.

**Request body**

| Field | Type | Required | Default | Constraints | Description |
|-------|------|:---:|-------|------------|-------------|
| `query` | string | ✓ | — | Max 4 096 chars | Natural language question |
| `kb_id` | string | ✓ | — | Non-empty | Knowledge base identifier |
| `user_id` | string | ✓ | — | Non-empty | Caller identifier |
| `query_mode` | string | | `"naive"` | See table below | Retrieval strategy |
| `max_results` | integer | | `20` | ≥ 1 | Max chunks in `data[]` |
| `top_k` | integer | | *(config)* | ≥ 1 | Vector candidate pool size |

**Query modes**

| `query_mode` | Strategy |
|---|---|
| `naive` | Parallel vector + KG retrieval, weighted scoring. Fastest; good default. |
| `rerank` | BM25 + vector RRF fusion, followed by cross-encoder reranking. Highest precision. |
| `rewriter` | LLM generates 3 query variations, runs parallel retrieval, aggregates results. Best for ambiguous queries. |
| `multihop` | ReAct reasoning loop (max 3 rounds). Use for multi-step questions. |
| `direct` | LLM answers from parametric knowledge only — no retrieval. `data[]` and `citations` will be empty. |

**Response fields**

| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | `true` on success |
| `code` | integer | `200` success · `400` bad request · `500` server error |
| `message` | string | Status message |
| `response` | string\|null | LLM-synthesised answer |
| `data[]` | array | Retrieved chunks (see below) |
| `total_count` | integer | Number of chunks in `data[]` |
| `citations[]` | array\|null | File-level source references (see below) |
| `query_info` | object | Diagnostic timing and query metadata |
| `request_id` | string | UUID for log tracing |

**`data[]` item**

| Field | Type | Description |
|-------|------|-------------|
| `kb_id` | string | Knowledge base ID |
| `kb_name` | string | Source filename or dataset name |
| `score` | float | Relevance score |
| `content` | string | Chunk text |
| `source_id` | string[] | Chunk ID(s) |

**`citations[]` item**

| Field | Type | Description |
|-------|------|-------------|
| `index` | integer | Citation number referenced in `response` |
| `source_file` | string | Original filename |
| `dataset` | string | Dataset the file belongs to |
| `file_url` | string\|null | Presigned download URL; `null` if S3 is not configured |

**Errors**

| `code` | Cause |
|--------|-------|
| `400` | Empty `query`, missing `kb_id` / `user_id`, or `query_mode` not supported for this KB's pipeline |
| `500` | Retrieval or LLM error |

```bash
curl -X POST http://<host>:8000/api/v1/hetadb/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "什么是知识图谱？", "kb_id": "tech-kb", "user_id": "agent"}'
```

---

## HetaDB — Datasets

### GET /api/v1/hetadb/files/raw-files/datasets

List all dataset names.

**Response**
```json
{ "success": true, "data": ["dataset-a", "dataset-b"] }
```

---

### POST /api/v1/hetadb/files/raw-files/datasets

Create an empty dataset.

**Request body**

| Field | Type | Required | Constraints |
|-------|------|:---:|------------|
| `name` | string | ✓ | `^[A-Za-z0-9_\-]+$`, max 64 chars |

**Errors** — `409` if the dataset already exists.

---

### GET /api/v1/hetadb/files/raw-files/datasets/{dataset}/files

List all files in a dataset. Hidden directories (`.uploads/`) are excluded.

**Response**
```json
{
  "success": true,
  "dataset": "my-dataset",
  "files": [
    { "name": "report.pdf", "size": 204800, "modified_time": "2026-03-24T10:00:00" }
  ]
}
```

**Errors** — `404` if dataset not found.

---

### POST /api/v1/hetadb/files/raw-files/datasets/{dataset}/files

Upload one or more files via `multipart/form-data`. For development and small files. Use the [chunked upload](#post-apiv1hetadbfilesraw-filesdatasetsdatasetuploadinit) flow for production or large files.

**Form fields**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `files` | file[] | ✓ | One or more files |

**Response**
```json
{
  "success": true,
  "message": "2 file(s) uploaded",
  "dataset": "my-dataset",
  "files": [{ "filename": "report.pdf", "size": 204800 }]
}
```

> Files with duplicate names are renamed automatically (`file.pdf` → `file_1.pdf`).
> If S3 is configured, the file is also uploaded to object storage. On S3 failure the local file is deleted and HTTP 500 is returned.

---

### DELETE /api/v1/hetadb/files/raw-files/datasets/{dataset}/files/{filename}

Delete a file. The local file is removed first; S3 deletion follows as a best-effort operation (failure logged as warning, does not fail the request).

**Errors** — `404` if file not found.

---

### DELETE /api/v1/hetadb/files/raw-files/datasets/{dataset}

Delete a dataset and all its files. Bulk-deletes S3 objects under `{dataset}/` prefix if S3 is configured.

**Errors** — `404` if not found · `409` if active parse tasks exist (cancel them first).

---

### Chunked Upload

Use for large files. Three-step flow: **init → upload chunks (parallel) → complete**.

#### POST /api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/init

Create an upload session.

**Request body**

| Field | Type | Required | Constraints | Description |
|-------|------|:---:|------------|-------------|
| `filename` | string | ✓ | Max 255 chars | Target filename |
| `total_chunks` | integer | ✓ | 1 – 10 000 | Number of chunks |
| `total_size` | integer | ✓ | ≥ 1 | Total file size in bytes |

**Response**
```json
{ "upload_id": "550e8400-e29b-41d4-a716-446655440000" }
```

**Errors** — `404` if dataset not found.

---

#### POST /api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/{upload_id}/chunk

Upload a single raw-binary chunk. Send the file slice as the raw request body (not multipart). Chunks may be uploaded in parallel.

**Query parameters**

| Parameter | Type | Required | Description |
|-----------|------|:---:|-------------|
| `chunk_index` | integer | ✓ | Zero-based chunk index |

**Response**
```json
{ "received": 0 }
```

**Errors** — `400` empty body or index out of range · `404` session not found.

---

#### POST /api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/{upload_id}/complete

Verify all chunks are present, merge them into the final file, clean up the session.

**Response**
```json
{ "success": true, "message": "Upload complete", "data": { "filename": "video.mp4", "size": 1073741824 } }
```

**Errors** — `400` missing chunks · `404` session not found.

---

#### DELETE /api/v1/hetadb/files/raw-files/datasets/{dataset}/upload/{upload_id}

Abort an in-progress upload and delete all temporary chunk files.

---

## HetaDB — Knowledge Bases

### GET /api/v1/hetadb/files/knowledge-bases

List all knowledge bases.

**Response**
```json
{
  "success": true,
  "data": [
    { "name": "tech-kb", "created_at": "2026-03-01T10:00:00Z", "status": "ready" }
  ]
}
```

> `status` is either `"ready"` (queryable) or `"deleting"` (skip — async deletion in progress). There is no processing/pending status at the KB level; dataset parse progress is tracked via tasks.

---

### POST /api/v1/hetadb/files/knowledge-bases

Create an empty knowledge base.

**Request body**

| Field | Type | Required | Constraints |
|-------|------|:---:|------------|
| `name` | string | ✓ | `^[A-Za-z0-9_\-]+$`, max 64 chars |

**Errors** — `409` if already exists.

---

### GET /api/v1/hetadb/files/knowledge-bases/{kb_name}

Get KB detail including dataset parse statuses and available query modes.

**Response**
```json
{
  "success": true,
  "name": "tech-kb",
  "created_at": "2026-03-01T10:00:00Z",
  "status": "ready",
  "datasets": [
    { "name": "ai-papers", "parsed": true, "process_mode": 0, "parsed_at": "2026-03-02T12:00:00Z" }
  ],
  "available_query_modes": [
    { "id": "naive", "label": "Naive", "desc": "Vector + KG retrieval" }
  ]
}
```

**Errors** — `404` if not found.

---

### GET /api/v1/hetadb/files/knowledge-bases/{kb_name}/overview

Generate a structured KB overview used internally by HetaGen tree generation. Returns both a data object and a pre-formatted LLM prompt string.

**Query parameters**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `top_nodes` | integer | `20` | Top entity nodes to include |
| `sample_relations` | integer | `15` | Sample relations to include |

**Response**
```json
{
  "success": true,
  "overview": { "datasets": [...], "top_entities": [...], "sample_relations": [...] },
  "prompt": "Knowledge Base Overview:\n..."
}
```

---

### DELETE /api/v1/hetadb/files/knowledge-bases/{kb_name}

Initiate asynchronous KB deletion. Returns **202** immediately. A daemon thread cancels active tasks, waits for termination, purges Milvus collections and PostgreSQL tables, then removes the filesystem directory.

**Query parameters**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `purge_db` | boolean | `true` | Also purge Milvus and PostgreSQL data |

**Response** — `202 Accepted`
```json
{ "success": true, "message": "Knowledge base 'tech-kb' deletion started" }
```

> Poll `GET /knowledge-bases/{kb_name}` until 404 to confirm deletion is complete.
> If `status` is already `"deleting"`, returns 200 without starting a duplicate thread.

---

### DELETE /api/v1/hetadb/files/knowledge-bases/{kb_name}/datasets/{dataset_name}

Remove a single parsed dataset from a KB. Purges Milvus and PostgreSQL data for that dataset. Raw files under `raw_files/{dataset}/` are **not** touched.

**Errors** — `404` KB or dataset not found · `409` active parse task exists.

---

### POST /api/v1/hetadb/files/knowledge-bases/{kb_name}/parse

Trigger document processing for one or more datasets into a KB. Each dataset becomes an independent background task.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|:---:|---------|-------------|
| `datasets` | string[] | ✓ | — | Dataset names from `raw_files/` to parse |
| `mode` | integer | | `0` | Processing pipeline mode (must be consistent across all datasets in the KB) |
| `schema_name` | string\|null | | `null` | Custom entity schema (from `/api/v1/hetadb/schemas`) |
| `force` | boolean | | `false` | Overwrite already-parsed datasets. When `false`, returns 409 if any dataset was previously parsed. |

**Response**
```json
{
  "success": true,
  "message": "Processing started for 2 dataset(s)",
  "data": {
    "tasks": [
      { "task_id": "uuid-1", "dataset": "ai-papers" },
      { "task_id": "uuid-2", "dataset": "textbooks" }
    ],
    "mode": 0
  }
}
```

**Errors** — `404` KB or dataset not found · `409` already parsed (use `force=true`) or active task in progress.

```bash
curl -X POST http://<host>:8000/api/v1/hetadb/files/knowledge-bases/tech-kb/parse \
  -H "Content-Type: application/json" \
  -d '{"datasets": ["ai-papers"], "mode": 0}'
```

---

## HetaDB — Processing Tasks

### GET /api/v1/hetadb/files/processing/tasks

List `file_processing` tasks.

**Query parameters**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string\|null | `null` | Filter by status: `pending`, `running`, `completed`, `failed`, `cancelling`, `cancelled` |
| `limit` | integer | `50` | Max results |

**Response** — array of task objects (see below).

---

### GET /api/v1/hetadb/files/processing/tasks/{task_id}

Get a single task by ID.

**Task object**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | UUID |
| `status` | string | `pending` · `running` · `completed` · `failed` · `cancelling` · `cancelled` |
| `task_type` | string | `"file_processing"` |
| `metadata` | object | `{ kb_name, dataset, mode, schema_name }` |
| `created_at` | string | ISO 8601 |
| `updated_at` | string | ISO 8601 |
| `error` | string\|null | Error message on failure |

**Errors** — `404` if not found.

---

### POST /api/v1/hetadb/files/processing/tasks/{task_id}/cancel

Cancel a task.

- **PENDING** → cancelled immediately.
- **RUNNING** → transitions to `cancelling`; the pipeline stops at the next stage boundary, rolls back partial data, then transitions to `cancelled`.
- Terminal states (`completed`, `failed`, `cancelled`) → HTTP 400.

**Response**
```json
{ "success": true, "message": "Task cancellation requested — will stop at next stage boundary", "data": { "task_id": "...", "status": "cancelling" } }
```

---

### GET /api/v1/hetadb/files/processing/config

Get the current processing configuration (LLM, embedding, graph extraction parameters).

**Response**
```json
{
  "success": true,
  "message": "Config retrieved",
  "data": {
    "llm": { "model": "qwen-max", "max_concurrent": 5, "timeout": 120 },
    "embedding": { "model": "text-embedding-v3", "dim": 1024, "batch_size": 32 },
    "graph": { "chunk_size": 1024, "overlap": 128, "batch_size": 10, "max_workers": 4 }
  }
}
```

---

## HetaDB — Config

### GET /api/v1/hetadb/config

Get the full merged configuration from `config.yaml` and `db_config.yaml`.

**Response**
```json
{ "success": true, "message": "Config retrieved successfully", "data": { ... } }
```

---

### GET /api/v1/hetadb/config/{section}

Get a single configuration section (e.g., `llm`, `postgresql`, `chunk_config`).

**Errors** — HTTP `404` if section not found.

---

### POST /api/v1/hetadb/config/reload

Reload configuration from disk. Use after manually editing config files to refresh cached values.

**Response**
```json
{ "success": true, "message": "Config reloaded successfully" }
```

---

## HetaDB — Schemas

Custom entity schemas control which entity types and attributes are extracted during KB parsing. Schemas are referenced by name in the [parse endpoint](#post-apiv1hetadbfilesknowledge-baseskb_nameparse).

### POST /api/v1/hetadb/schemas

Create an entity schema. Returns HTTP **201** on success.

**Request body**

| Field | Type | Required | Constraints | Description |
|-------|------|:---:|------------|-------------|
| `name` | string | ✓ | `^[A-Za-z0-9_\-]+$` | Schema name |
| `entities` | EntityDefinition[] | ✓ | Min 1 item | Entity definitions |

**EntityDefinition**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `type` | string | ✓ | Top-level type. Must be one of: `客观实体`, `抽象实体`, `事件实体`, `文献实体` |
| `subtype` | string | ✓ | SubType name recognised by the KG extraction prompt |
| `attributes` | string[] | | Attribute names to extract for this SubType |

**Errors** — `409` if schema already exists · `422` if `type` is invalid.

```json
{
  "name": "medical-schema",
  "entities": [
    { "type": "客观实体", "subtype": "疾病", "attributes": ["症状", "治疗方法"] },
    { "type": "事件实体", "subtype": "临床试验", "attributes": ["时间", "地点"] }
  ]
}
```

---

### GET /api/v1/hetadb/schemas

List all schemas (name, created_at, entity_count).

---

### GET /api/v1/hetadb/schemas/{name}

Get schema detail including all entity definitions and a `prompt_preview` string showing how the schema will be injected into the extraction prompt.

**Errors** — HTTP `404` if not found.

---

### DELETE /api/v1/hetadb/schemas/{name}

Delete a schema. Does not affect KBs already parsed with it.

**Errors** — HTTP `404` if not found.

---

## HetaGen — Table Generation

### POST /api/v1/hetagen/pipeline/submit

Submit a text-to-table + text-to-SQL task. Returns immediately with a `task_id`; poll status for results.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|:---:|---------|-------------|
| `question` | string | ✓ | — | Natural language table question (e.g., "List the top 10 NASDAQ companies by market cap") |
| `sql_question` | string\|null | | `null` | Override SQL question if different from `question` |
| `top_k` | integer | | `5` | Vector search candidate pool per query |
| `threshold` | float | | `0.5` | Minimum similarity score for retrieval |
| `max_workers` | integer | | `16` | Max concurrent retrieval threads |

**Response**
```json
{ "task_id": "abc123", "status": "pending", "message": "Task submitted" }
```

---

### GET /api/v1/hetagen/pipeline/status/{task_id}

Poll task status. Returns result when completed.

**Response — completed**
```json
{
  "task_id": "abc123",
  "status": "completed",
  "result": {
    "table_name": "nasdaq_top10",
    "schema": { "title": "...", "entities": [...], "columns": [...] },
    "csv_data": [{ "公司": "Apple", "市值（美元）": "3.2万亿" }],
    "sql": "SELECT * FROM nasdaq_top10 ORDER BY 市值 DESC LIMIT 10",
    "query_results": [...]
  }
}
```

**Response — failed**
```json
{ "task_id": "abc123", "status": "failed", "error": "..." }
```

**Errors** — HTTP `404` if task not found.

---

### WS /api/v1/hetagen/pipeline/stream

Stream pipeline execution with real-time progress updates via WebSocket.

**Client → Server** (JSON after connection):
```json
{ "question": "...", "sql_question": null, "top_k": 5, "threshold": 0.5, "max_workers": 16 }
```

**Server → Client** messages:

| `type` | Payload | Description |
|--------|---------|-------------|
| `progress` | `{ "step": 2 }` | Pipeline step number |
| `result` | `{ "data": { table_name, schema, csv_data, sql, query_results } }` | Final result |
| `error` | `{ "message": "..." }` | Execution error |

---

## HetaGen — Tag Tree

### POST /api/v1/hetagen/tag-tree/generate

Submit a knowledge tree generation task. Returns immediately; poll `/status/{task_id}`.

**Request body**

| Field | Type | Required | Constraints | Description |
|-------|------|:---:|------------|-------------|
| `topic` | string | ✓ | Non-empty | Domain topic (e.g., `"糖尿病诊疗"`) |
| `mode` | string | ✓ | `"kb"` or `"pure_llm"` | Generation strategy |
| `kb_name` | string\|null | ✓ when `mode="kb"` | — | HetaDB knowledge base to use as grounding context |

**Modes**

| Mode | Description |
|------|-------------|
| `kb` | Grounds the tree in real KB entities. Requires `kb_name`. Produces more accurate domain descriptions. |
| `pure_llm` | Uses LLM world knowledge only. No KB dependency; faster. |

**Response**
```json
{ "task_id": "xyz789", "status": "pending", "message": "Tree generation started (mode=kb)" }
```

---

### POST /api/v1/hetagen/tag-tree/submit *(legacy)*

Upload an Excel file and parse its hierarchical path structure into a tag tree. Each row represents a leaf node; column values form the hierarchy path.

**Form fields** (`multipart/form-data`)

| Field | Type | Required | Default | Description |
|-------|------|:---:|---------|-------------|
| `file` | file | ✓ | — | `.xlsx` or `.xls` file |
| `tree_name` | string | | `"tag_tree"` | Tree name |
| `tree_description` | string | | `""` | Tree description |
| `sheet_name` | string | | `"0"` | Sheet name or zero-based index |

**Response**
```json
{ "task_id": "...", "status": "pending", "message": "Task submitted" }
```

---

### GET /api/v1/hetagen/tag-tree/status/{task_id}

Poll task status. Falls back to disk if the task is no longer in memory (e.g., after server restart).

**Response — completed**
```json
{
  "task_id": "xyz789",
  "status": "completed",
  "result": {
    "tree_name": "糖尿病诊疗",
    "tree_description": "基于知识库生成的领域知识树",
    "node_count": 42,
    "nodes": [
      {
        "node_name": "预防管理",
        "category": "糖尿病诊疗 -> 预防管理",
        "description": "预防糖尿病发生发展的各项措施...",
        "children": [...]
      }
    ]
  }
}
```

**Errors** — HTTP `404` if task not found and no on-disk result exists.

---

## HetaMem — MemoryKB

Agent-owned knowledge graph backed by LightRAG with local file storage (NanoVectorDB + NetworkX). Separate from HetaDB's Milvus/PostgreSQL infrastructure.

### POST /api/v1/hetamem/kb/insert

Queue a memory entry for insertion. Returns **202** immediately. LightRAG entity extraction and graph construction run in the background.

> Content typically takes **10–60 seconds** to become searchable after insert. Do not query immediately after inserting.

**Request** (`multipart/form-data`)

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `query` | string | ✓ | Text content to insert |
| `video` | file\|null | | Video file; transcribed via DashScope and merged into text |
| `audio` | file\|null | | Audio file; transcribed via speech-to-text |
| `image` | file\|null | | Image file; captioned via OCR |

**Response** — `202 Accepted`
```json
{
  "id": "job-uuid",
  "query": "Transformers revolutionized NLP...",
  "status": "accepted",
  "videocaption": null,
  "audiocaption": null,
  "imagecaption": null
}
```

**Errors** — HTTP `400` if `query` is empty.

---

### POST /api/v1/hetamem/kb/query

Query the knowledge graph and receive a synthesised answer.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|:---:|---------|-------------|
| `query` | string | ✓ | — | Natural language question |
| `mode` | string | | `"hybrid"` | Retrieval strategy (see table below) |
| `use_pm` | boolean | | `false` | Query parametric memory model first; skip RAG if relevant |

**Retrieval modes**

| Mode | Description |
|------|-------------|
| `hybrid` | Combines local entity recall + global community summaries. Default. |
| `local` | Entity neighbourhood + 1-hop relations. Best for specific entity queries. |
| `global` | Community summaries across full graph. Best for thematic/high-level questions. |
| `naive` | Simple chunk similarity. Fastest. |

**Response**
```json
{
  "query": "What are Transformers?",
  "mode": "hybrid",
  "pm_used": false,
  "pm_memory": null,
  "pm_relevant": false,
  "rag_memory": "[Entity: Transformer]\nDescription: ...\n[Relations]: ...",
  "final_answer": "Transformers are neural network architectures..."
}
```

> Read `final_answer`. If `rag_memory` is empty or `final_answer` expresses uncertainty, the knowledge base may not yet contain relevant content (allow more time after recent inserts).

**Errors** — HTTP `400` empty query · HTTP `500` retrieval failure.

---

## HetaMem — MemoryVG

Personal episodic memory backed by Milvus (vector store), optional Neo4j (graph store), and SQLite (modification history). All operations are scoped to at least one of `user_id`, `agent_id`, or `run_id`. In single-agent deployments, use `agent_id: "agent"` consistently.

### POST /api/v1/hetamem/vg/add

Extract facts from a conversation and store them. The LLM automatically extracts structured facts, compares them against existing memories, and decides to ADD, UPDATE, or DELETE each one.

**Request body**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `messages` | Message[] | ✓ | Conversation turns. At least one scope ID required. |
| `agent_id` | string\|null | ✓† | Scope identifier |
| `user_id` | string\|null | ✓† | Scope identifier |
| `run_id` | string\|null | ✓† | Scope identifier |
| `metadata` | object\|null | | Additional key-value tags stored with each memory |

† At least one of `agent_id`, `user_id`, `run_id` must be provided.

**Message object**

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | `"user"` or `"assistant"` |
| `content` | string | Message text |

**Response**
```json
{
  "results": [
    { "id": "uuid1", "memory": "Agent prefers concise answers", "event": "ADD" },
    { "id": "uuid2", "memory": "Agent uses Python", "event": "UPDATE", "previous_memory": "Agent codes" }
  ],
  "relations": { "added_entities": [...], "deleted_entities": [...] }
}
```

Memories are available for search **immediately** after add.

**Errors** — HTTP `422` if no scope ID provided · HTTP `500` on failure.

```bash
curl -X POST http://<host>:8000/api/v1/hetamem/vg/add \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "I prefer concise answers"}],
    "agent_id": "agent"
  }'
```

---

### POST /api/v1/hetamem/vg/search

Search memories by semantic similarity.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|:---:|---------|-------------|
| `query` | string | ✓ | — | Natural language query |
| `agent_id` | string\|null | ✓† | — | Scope filter |
| `user_id` | string\|null | ✓† | — | Scope filter |
| `run_id` | string\|null | ✓† | — | Scope filter |
| `limit` | integer | | `10` | Max results |
| `threshold` | float\|null | | `null` | Minimum similarity score (0–1). `null` = no filter. |

† At least one required.

**Response**
```json
{
  "results": [
    { "id": "uuid1", "memory": "Agent prefers concise answers", "score": 0.92, "agent_id": "agent", "created_at": "..." }
  ],
  "relations": [...]
}
```

**Errors** — HTTP `400` empty query · HTTP `422` no scope ID.

---

### GET /api/v1/hetamem/vg

List all memories matching the scope filter.

**Query parameters**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_id` | string\|null | `null` | Scope filter |
| `user_id` | string\|null | `null` | Scope filter |
| `run_id` | string\|null | `null` | Scope filter |
| `limit` | integer | `100` | Max results |

**Response**
```json
{ "results": [{ "id": "...", "memory": "...", "agent_id": "agent", "created_at": "..." }] }
```

---

### GET /api/v1/hetamem/vg/{memory_id}

Retrieve a single memory entry.

**Response**
```json
{ "id": "uuid1", "memory": "Agent prefers concise answers", "hash": "...", "created_at": "...", "agent_id": "agent", "metadata": {} }
```

**Errors** — HTTP `404` if not found.

---

### GET /api/v1/hetamem/vg/{memory_id}/history

Return the full modification history (ADD / UPDATE / DELETE events) for a memory.

**Response** — array of history records:
```json
[
  { "id": "hist-1", "memory_id": "uuid1", "event": "ADD", "old_memory": null, "new_memory": "Agent prefers concise answers", "created_at": "...", "is_deleted": false }
]
```

---

### PUT /api/v1/hetamem/vg/{memory_id}

Update the text content of a memory. Re-embeds with the new text.

**Request body**

| Field | Type | Required | Description |
|-------|------|:---:|-------------|
| `data` | string | ✓ | New memory text (non-empty) |

**Response**
```json
{ "message": "Memory updated successfully!" }
```

**Errors** — HTTP `400` empty `data`.

---

### DELETE /api/v1/hetamem/vg/{memory_id}

Delete a single memory entry. Logged in modification history.

**Response**
```json
{ "message": "Memory deleted successfully!" }
```

---

### DELETE /api/v1/hetamem/vg

Delete all memories matching the scope filter.

**Query parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent_id` | string\|null | Scope filter |
| `user_id` | string\|null | Scope filter |
| `run_id` | string\|null | Scope filter |

**Response**
```json
{ "message": "Memories deleted successfully!" }
```
