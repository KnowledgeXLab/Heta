# HetaWiki

HetaWiki is Heta's LLM-native wiki layer. It compiles uploaded source documents
into a versioned Markdown knowledge base and lets both users and agents browse,
merge, and query that wiki directly.

---

## Supported Formats

HetaWiki currently accepts the same lightweight document set used by its ingest
pipeline:

| Format | Extensions |
|--------|-----------|
| Plain text / markup | `.md`, `.markdown`, `.txt` |
| PDF | `.pdf` |
| Word documents | `.docx`, `.doc` |
| Presentations | `.pptx`, `.ppt` |

LibreOffice is required to convert `.doc`, `.ppt`, and `.pptx` into PDF before
parsing.

---

## Core Concepts

| Component | Role |
|---|---|
| `raw/` | Immutable source files uploaded by the user |
| `wiki/pages/` | Markdown pages that act as the source of truth for downstream reading and querying |
| `index.md` | Global wiki directory used by both the UI and the LLM |
| `log.md` | Append-only operation log for ingest, merge, and maintenance tasks |
| standalone git repo | Tracks wiki evolution independently from the main Heta codebase |

---

## Two Ingest Modes

| Mode | What it does | Best for |
|---|---|---|
| `default ingest` | Parses one source document and turns it into exactly one new wiki page | Fast document onboarding |
| `merge ingest` | Runs an agent loop against a working copy of the wiki, reading and updating existing pages or creating new ones as needed | High-quality integration into an existing wiki |

Both modes are asynchronous. The API returns a `task_id`; callers poll the task
endpoint until the operation completes.

---

## Query and Navigation

HetaWiki exposes three complementary ways to consume the wiki:

- `POST /api/v1/hetawiki/query` — asynchronous wiki question answering
- `GET /api/v1/hetawiki/index` — fetch the current wiki directory
- `GET /api/v1/hetawiki/pages/{filename}` — read a single page
- `GET /api/v1/hetawiki/graph` — materialise wiki links as nodes and edges for graph visualisation

This makes HetaWiki usable both as a Web UI feature and as an agent-readable
knowledge layer.

---

## Maintenance

HetaWiki also includes a configurable lint scheduler. Lint jobs scan the wiki
periodically to surface structural issues such as inconsistent pages, missing
links, or stale content candidates.

The scheduler is controlled via `POST /api/v1/hetawiki/lint`.

---

## Related Pages

- [Ingesting Documents](ingest.md) — user guide for the frontend upload flow and ingest mode selection
- [Querying the Wiki](query.md) — ask questions, inspect source pages, and continue with follow-up questions
- [REST API Reference](../reference/api.md)
- [Configuration Reference](../reference/config.md)
