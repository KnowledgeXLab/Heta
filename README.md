<p align="center">
  <img src="docs/images/banner.png" width="800" alt="Heta — Agent-Oriented Knowledge Management Platform"/>
</p>

<p align="center">
  <b>Agent-Oriented Knowledge Management Platform</b><br/>
  Unified knowledge base, episodic memory, and generative synthesis.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/License-AGPL%20v3-blue"/>
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white"/>
  <img src="https://img.shields.io/github/stars/KnowledgeXLab/Heta?style=social"/>
</p>

---

## What is Heta?

Heta is an all-in-one knowledge infrastructure for AI agents. It gives agents a place to store, retrieve, and accumulate knowledge across three complementary layers:

- **HetaDB** — ingest documents (PDF, DOCX, PPTX, XLS/XLSX, images, …), extract knowledge graphs, and query with five retrieval strategies from naive vector search to multi-hop reasoning.
- **HetaMem** — a dual-layer memory system: fast episodic recall (MemoryVG) for conversation facts, and a long-term knowledge graph (MemoryKB) that grows with the agent.
- **HetaGen** — knowledge-base-driven structured content generation: table synthesis, tag-tree construction, and Text-to-SQL. *(early stage)*

---

## Key Features

**HetaDB**
- Ingests PDF, DOCX, PPTX, XLS/XLSX, images, HTML, Markdown, ZIP archives
- LLM-powered knowledge graph extraction with deduplication (Union-Find merging)
- Five query strategies: `naive` · `rerank` (BM25 + vector + cross-encoder) · `rewriter` · `multihop` (ReAct) · `direct`
- Inline citations linking answers back to source documents

**HetaMem**
- **MemoryVG** — LLM auto-extracts facts from conversations; instant semantic search; full CRUD + history audit
- **MemoryKB** — LightRAG knowledge graph that grows as the agent learns; `hybrid` / `local` / `global` retrieval modes
- Scope isolation per `user_id` / `agent_id` / `run_id`

**HetaGen** *(early stage)*
- Generate structured tables by querying the knowledge base
- Tag-tree construction from topics
- Text-to-SQL over generated tables

> HetaDB and HetaMem also expose optional MCP servers (ports 8012 / 8011) for direct integration with MCP-compatible clients such as Claude Desktop and Cursor.

---

## Quick Start

### Option A — Docker Compose (recommended)

**Prerequisites:** Docker ≥ 24.0 · Docker Compose ≥ 2.20 · DashScope and SiliconFlow API keys

```bash
git clone https://github.com/HetaTeam/Heta.git
cd Heta

# Chinese API providers (DashScope + SiliconFlow)
cp config.example.zh.yaml config.yaml

# International API providers (OpenAI + Gemini)
# cp config.example.yaml config.yaml
```

Open `config.yaml` and fill in your API keys:

```yaml
providers:
  dashscope:
    api_key: "YOUR_DASHSCOPE_KEY"    # required

  siliconflow:
    api_key: "YOUR_SILICONFLOW_KEY"  # required
```

```bash
docker-compose up -d
```

First run pulls images and builds the stack (~10–20 min). Verify:

```bash
docker-compose ps           # all services: healthy
curl localhost:8000/health
```

| URL | Description |
|---|---|
| http://localhost | Heta web UI |
| http://localhost:8000/docs | REST API (Swagger) |
| http://localhost:7474 | Neo4j browser |
| http://localhost:9001 | MinIO console |

```bash
docker-compose down         # stop, keep data
docker-compose down -v      # stop and delete all volumes
```

---

### Option B — Manual Setup

**Prerequisites:** Python 3.10 · PostgreSQL · Milvus · Neo4j · LibreOffice

> LibreOffice is required for HetaDB and HetaWiki to parse `.doc`, `.docx`, `.ppt`, and `.pptx` files.
> Install it with your system package manager:
> ```bash
> # Ubuntu / Debian
> sudo apt-get install -y libreoffice-writer libreoffice-impress libreoffice-calc
>
> # macOS
> brew install --cask libreoffice
> ```

```bash
# 1. Install backend
conda create -n heta python=3.10 -y && conda activate heta
pip install -e .

# 2. Build frontend
cd heta-frontend && npm install && npm run build && cd ..

# 3. Run (unified — all modules on one port)
PYTHONPATH=src python src/main.py          # → http://localhost:8000
```

**Run each module independently:**

```bash
export PYTHONPATH=/path/to/Heta/src

python src/hetadb/api/main.py              # HetaDB   → :8001
python src/hetagen/api/main.py             # HetaGen  → :8002
python src/hetamem/api/main.py             # HetaMem  → :8003

# MCP servers
HETAMEM_BASE_URL=http://localhost:8000 python src/hetamem/mcp/server.py  # → :8011
HETADB_BASE_URL=http://localhost:8000  python src/hetadb/mcp/server.py   # → :8012
```

**Port reference:**

| Service | Port |
|---|---|
| Heta unified API | 8000 |
| HetaDB (standalone) | 8001 |
| HetaGen (standalone) | 8002 |
| HetaMem (standalone) | 8003 |
| HetaMem MCP | 8011 |
| HetaDB MCP | 8012 |
| PostgreSQL | 5432 |
| Milvus | 19530 |
| Neo4j Browser / Bolt | 7474 / 7687 |
| MinIO S3 / Console | 9000 / 9001 |

---

### Connecting Agents to Heta

Heta exposes two integration layers — use one or both:

#### Via MCP — Tool Access

MCP gives your agent direct tool-call access to HetaDB and HetaMem. Add the following to your MCP client config (e.g. Claude Desktop `~/.claude.json`):

```json
{
  "mcpServers": {
    "hetamem": { "type": "http", "url": "http://localhost:8011/mcp/" },
    "hetadb":  { "type": "http", "url": "http://localhost:8012/mcp/" }
  }
}
```

The agent can now call HetaDB and HetaMem tools directly without any additional setup.

#### Via Skill — Workflow Guidance

The bundled skill teaches the agent *when* and *how* to use each layer — which system to query first, how to store findings, and the correct three-step retrieval order (MemoryVG → HetaDB → MemoryKB).

```
skills/querying-knowledge-and-memory/SKILL.md
```

Load it in your agent system!

---

## Core Workflows

### HetaDB — Build a Knowledge Base and Chat

Datasets and knowledge bases are created and managed through the **Heta web UI** (`http://localhost`):

1. Create a dataset and upload your documents (PDF, DOCX, HTML, images, …)
2. Create a knowledge base and link it to your dataset
3. Trigger parsing — Heta extracts a knowledge graph and embeddings (async)

Once indexed, agents query the knowledge base via the chat API:

```bash
# List available knowledge bases
curl http://localhost:8000/api/v1/hetadb/files/knowledge-bases

# Query a knowledge base
curl -X POST http://localhost:8000/api/v1/hetadb/chat \
  -H "Content-Type: application/json" \
  -d '{
    "query":      "What are the main contributions?",
    "kb_id":      "research-kb",
    "user_id":    "agent",
    "query_mode": "rerank"
  }'
# → { "response": "...", "citations": [...] }
```

Available `query_mode` values: `naive` · `rerank` · `rewriter` · `multihop` · `direct`

---

### MemoryVG — Episodic Memory

```bash
# Store facts extracted from a conversation
curl -X POST http://localhost:8000/api/v1/hetamem/vg/add \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user",      "content": "I prefer concise Python examples."},
      {"role": "assistant", "content": "Noted."}
    ],
    "agent_id": "agent"
  }'

# Recall relevant facts
curl -X POST http://localhost:8000/api/v1/hetamem/vg/search \
  -H "Content-Type: application/json" \
  -d '{"query": "user coding preferences", "agent_id": "agent"}'
# → { "results": [{"memory": "Prefers concise Python examples", "score": 0.91}] }
```

---

### MemoryKB — Long-Term Knowledge Graph

```bash
# Insert knowledge into the agent's long-term graph (async — ~200s to index)
curl -X POST http://localhost:8000/api/v1/hetamem/kb/insert \
  -F "query=Transformer models use self-attention to process sequences in parallel."

# Query the knowledge graph
curl -X POST http://localhost:8000/api/v1/hetamem/kb/query \
  -H "Content-Type: application/json" \
  -d '{"query": "How do transformers handle long-range dependencies?", "mode": "hybrid"}'
# → { "final_answer": "..." }
```

---

## Using the Querying Skill

The bundled [querying skill](skills/querying-knowledge-and-memory/SKILL.md) encodes the recommended retrieval workflow — when to use each layer, in what order, and what to store back. Load it into any skill-aware agent system and it will orchestrate HetaDB, MemoryVG, and MemoryKB automatically.

**Three-step retrieval order the skill enforces:**

```
1. MemoryVG  — check fast personal memory first (~100 ms)
2. HetaDB    — deep retrieval from uploaded documents (1–3 s)
3. Store back — cache finding in MemoryVG, or insert into MemoryKB if worth accumulating
```

**Layer selection guide:**

| Layer | Best for | Typical latency |
|---|---|---|
| MemoryVG | Facts already seen; cross-session cache | ~100 ms |
| HetaDB | Deep retrieval from uploaded documents | 1–3 s |
| MemoryKB | Agent's accumulating knowledge graph | ~200 s to index · ~1 s to query |

---

## Project Structure

```
Heta/
├── config.example.yaml       # Config template (international: OpenAI / Gemini)
├── config.example.zh.yaml    # Config template (domestic: DashScope / SiliconFlow)
├── docker-compose.yml        # Full-stack deployment
├── Dockerfile
├── pyproject.toml
├── docs/                     # API reference and design documents
├── heta-frontend/            # Web UI
├── skills/                   # Agent skills
│   └── querying-knowledge-and-memory/
└── src/
    ├── main.py               # Unified entry point (port 8000)
    ├── common/               # Shared utilities: logging, config, LLM client, tasks
    ├── hetadb/               # Knowledge-base ingestion & multi-strategy chat
    ├── hetagen/              # Table and tag-tree generation
    └── hetamem/              # Agent memory: MemoryKB + MemoryVG + MCP server
```

---

## Acknowledgments

Heta is built on the shoulders of excellent open-source projects:

- **[MinerU](https://github.com/opendatalab/MinerU)** — the document parsing engine powering HetaDB ingestion
- **[mem0](https://github.com/mem0ai/mem0)** — the episodic memory engine powering MemoryVG
- **[LightRAG](https://github.com/HKUDS/LightRAG)** — the knowledge graph engine powering MemoryKB

We are grateful to their authors for making this work possible.

HetaMem draws from **[MemVerse](https://github.com/KnowledgeXLab/MemVerse)**, our research framework for multimodal lifelong agent memory. If you are interested in agent memory, we recommend checking it out.

---

## License

AGPL-3.0 — see [LICENSE](LICENSE) for details.

This project incorporates code from the following open-source projects:

- **[MinerU](https://github.com/opendatalab/MinerU)** — AGPL-3.0 License
- **[LightRAG](https://github.com/HKUDS/LightRAG)** — MIT License
- **[mem0](https://github.com/mem0ai/mem0)** — Apache 2.0 License
