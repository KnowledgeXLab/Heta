<p align="center">
  <img src="images/banner.png" width="800" alt="Heta — Agent-Oriented Knowledge Management Platform"/>
</p>

<p align="center">
  <b>Agent-Oriented Knowledge Management Platform</b><br/>
  Multi-database orchestration · Dual-mode memory · Structured knowledge generation
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/License-AGPL%20v3-blue"/>
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white"/>
</p>

---

## What is Heta?

Heta is an all-in-one knowledge infrastructure for AI agents. It integrates multiple underlying databases to help agents do three things: acquire external knowledge, accumulate their own memory, and reason and generate based on that knowledge.

- **HetaDB** — Unified management across multiple databases for smarter external knowledge integration. No need to worry about what format the knowledge comes from or which database it should go into.
- **HetaWiki** — Compiles uploaded documents into a versioned Markdown wiki that users and agents can browse, merge, and query directly.
- **HetaMem** — Dual-mode memory: fast episodic recall via vector search (MemoryVG), and a long-term knowledge graph that evolves continuously with the agent (MemoryKB).
- **HetaGen** — Synthesizes and expands upon existing knowledge bases to generate higher-value structured content.

---

## Key Features

**HetaDB**

- Multi-format file parsing via MinerU and other tools (PDF, HTML, images, sheets, Markdown, Office file formats, archives, etc.)
- Parsed knowledge is automatically distributed across the appropriate databases — no manual storage decisions required
- Multiple query strategies for different scenarios: `naive` (vector search)， `rerank` (BM25 + vector + cross-encoder)， `rewriter` (query pptimization)， `multihop` (ReAct)， `direct` (direct query)
- Source document tracing to support agentic search workflows

**HetaWiki**

- LLM-native wiki compilation from uploaded source files into versioned Markdown pages
- Dual ingest modes: fast `default ingest` for new pages and agentic `merge ingest` for precise wiki integration
- Direct wiki reading via index, page, and graph endpoints for both UI workflows and agent workflows
- Independent wiki git history for content evolution and rollback

**HetaMem**

- **MemoryVG** — Lightweight memory for high-frequency, fragmented information: quickly stores and retrieves conversation content, user preferences, and contextual facts; full CRUD + history audit
- **MemoryKB** — Empowers AI agents with continuous, multimodal memory by combining hierarchical knowledge graphs with fast parametric recall, enabling long-term context understanding and personalized, adaptive interactions.

**HetaGen** _(early stage)_

- Generates structured tabular data from knowledge bases and supports Text-to-SQL queries on the generated tables
- Automatically construct hierarchical knowledge structures (tag trees) from topics

!!! tip "MCP Integration"
HetaDB and HetaMem expose optional MCP servers (ports 8012 / 8011) for direct integration with Claude Desktop, Cursor, and other MCP-compatible clients.

---

## Quick Links

- [Docker Compose Quick Start](quick-start/docker.md) — recommended, full stack in one command
- [Manual Setup](quick-start/manual.md) — run modules independently
- [Connect MCP Clients](quick-start/mcp-clients.md) — Claude Desktop, Cursor
- [HetaWiki Overview](hetawiki/index.md)
- [REST API Reference](reference/api.md)
