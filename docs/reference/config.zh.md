# 配置参考

Heta 的配置由两个文件控制：

- **`config.yaml`**（项目根目录）— API 密钥、数据库连接、各模块的模型选择。从 `config.example.yaml` 复制后填入你的凭证。
- **`src/hetadb/config/db_config.yaml`** — 处理管线调优参数（批量大小、并发数、分块设置）。通常无需修改。

**必填与可选** — 标注 **必填** 的参数必须在启动前设置，其余均有可用默认值。

---

## config.yaml

### `providers`

通过 YAML 锚点在各模块间共享的凭证块。只需定义你实际使用的服务商，未使用的可留空或删除。

| 参数 | 是否必填 | 说明 |
|------|---------|------|
| `providers.dashscope.api_key` | 必填（国内） | 阿里云 DashScope API Key |
| `providers.dashscope.base_url` | — | DashScope OpenAI 兼容端点（已预填） |
| `providers.siliconflow.api_key` | 必填 | SiliconFlow API Key（用于向量化） |
| `providers.siliconflow.base_url` | — | SiliconFlow 端点（已预填） |
| `providers.openai.api_key` | 必填（国际） | OpenAI API Key |
| `providers.openai.base_url` | — | OpenAI 端点（已预填） |
| `providers.gemini.api_key` | 必填（国际） | Google Gemini API Key |
| `providers.gemini.base_url` | — | Gemini OpenAI 兼容端点（已预填） |

---

### `persistence`

三个后端存储的连接配置。使用 Docker Compose 时默认值已预配置，无需修改。

#### PostgreSQL

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `persistence.postgresql.host` | `postgres` | 主机名（Docker 外部使用 `localhost`） |
| `persistence.postgresql.port` | `5432` | 端口 |
| `persistence.postgresql.user` | `postgres` | 用户名 |
| `persistence.postgresql.password` | `postgres` | 密码 |
| `persistence.postgresql.database` | `postgres` | 数据库名 |

#### Milvus

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `persistence.milvus.host` | `milvus` | 主机名 |
| `persistence.milvus.port` | `19530` | gRPC 端口 |
| `persistence.milvus.url` | `http://milvus:19530` | 完整 URI，设置后覆盖 host/port |

#### Neo4j

仅 HetaMem MemoryVG 使用。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `persistence.neo4j.url` | `bolt://neo4j:7687` | Bolt 连接地址 |
| `persistence.neo4j.username` | `neo4j` | 用户名 |
| `persistence.neo4j.password` | `heta_password` | 密码 |
| `persistence.neo4j.database` | `neo4j` | 数据库名 |

---

### `hetadb`

#### `hetadb.llm`

用于图谱实体/关系抽取和答案生成的大语言模型。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hetadb.llm.model` | `qwen3-32b` | 模型名称 |
| `hetadb.llm.max_concurrent_requests` | `10` | 最大并发 LLM 调用数 |
| `hetadb.llm.max_retries` | `3` | 失败重试次数 |
| `hetadb.llm.timeout` | `120` | 请求超时时间（秒） |

#### `hetadb.vlm`

用于 PDF 页面和图片理解的视觉语言模型。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hetadb.vlm.model` | `qwen2.5-vl-72b-instruct` | VLM 模型名称 |
| `hetadb.vlm.max_concurrent_requests` | `10` | 最大并发 VLM 调用数 |
| `hetadb.vlm.max_retries` | `5` | 失败重试次数 |
| `hetadb.vlm.timeout` | `120` | 请求超时时间（秒） |

#### `hetadb.embedding_api`

用于文本块和节点向量索引的 Embedding 模型。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hetadb.embedding_api.model` | `BAAI/bge-m3` | Embedding 模型名称 |
| `hetadb.embedding_api.dim` | `1024` | 向量维度，必须与 Milvus 集合 Schema 一致 |
| `hetadb.embedding_api.timeout` | `30` | 请求超时时间（秒） |
| `hetadb.embedding_api.batch_size` | `2000` | 每批向量化队列的记录数 |
| `hetadb.embedding_api.num_threads` | `8` | 并行向量化工作线程数 |
| `hetadb.embedding_api.max_retries` | `5` | 失败重试次数 |
| `hetadb.embedding_api.retry_delay` | `2` | 初始重试等待时间（秒），每次翻倍 |

#### `hetadb.milvus`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hetadb.milvus.db_name` | `hetadb` | HetaDB 集合所在的 Milvus 数据库名 |
| `hetadb.milvus.sentence_mode` | `false` | 是否使用句子级分块（默认段落级） |

#### `hetadb.query_defaults`

调用方未指定时使用的默认检索参数。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hetadb.query_defaults.top_k` | `10` | 每次查询的向量候选池大小 |
| `hetadb.query_defaults.threshold` | `0.0` | 结果包含的最低相似度分数 |
| `hetadb.query_defaults.similarity_weight` | `1.5` | 向量相似度得分的权重系数 |
| `hetadb.query_defaults.occur_weight` | `1.0` | 知识图谱出现次数的权重系数 |
| `hetadb.query_defaults.reranker_url` | *(未设置)* | 自托管交叉编码器重排服务的地址。`rerank` 模式的完整功能依赖此配置；未设置时 `rerank` 模式将退化为纯 RRF 排序，不执行交叉编码器打分。接口规范：`POST /rerank`，请求体 `{"pairs": [["query", "doc"], ...]}`，响应体 `{"scores": [...]}`。推荐模型：Qwen3-Reranker。 |

#### `hetadb.search_params`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hetadb.search_params.ef_multiplier` | `10` | HNSW ef = top_k × ef_multiplier，越大越准确但越慢 |

---

### `hetamem`

#### `hetamem.memorykb`

MemoryKB 基于 LightRAG（NanoVectorDB + NetworkX），轻量低延迟。

| 参数 | 默认值 | 国内替代方案 |
|------|--------|------------|
| `hetamem.memorykb.llm.model` | `gpt-4o-mini-2024-07-18` | DashScope `qwen-plus` |
| `hetamem.memorykb.embedding.model` | `text-embedding-3-small` | DashScope `text-embedding-v4` |
| `hetamem.memorykb.embedding.dim` | `1536` | `1536` |

#### `hetamem.memoryvg`

MemoryVG 基于 Milvus + Neo4j，用于多轮对话的结构化事实存储。

| 参数 | 默认值 | 国内替代方案 |
|------|--------|------------|
| `hetamem.memoryvg.llm.config.model` | `qwen3-32b` | 同上 |
| `hetamem.memoryvg.embedder.config.model` | `text-embedding-3-large` | SiliconFlow `BAAI/bge-m3` |
| `hetamem.memoryvg.embedder.config.embedding_dims` | `1024` | `1024` |
| `hetamem.memoryvg.vector_store.config.collection_name` | `memoryvg` | Milvus 集合名称 |
| `hetamem.memoryvg.vector_store.config.db_name` | `hetamem` | Milvus 数据库名 |

!!! note
    `hetamem.memoryvg.graph_store` 直接继承 `persistence.neo4j` 的连接配置，无需单独设置。

---

### `hetagen`

| 参数 | 默认值 | 国内替代方案 |
|------|--------|------------|
| `hetagen.llm.model` | `gemini-3-flash-preview` | DashScope `qwen3-32b` |
| `hetagen.llm.max_concurrent_requests` | `10` | — |
| `hetagen.llm.timeout` | `120` | — |
| `hetagen.vlm.model` | `Qwen/Qwen3-VL-32B-Instruct` | 同上 |
| `hetagen.embedding_api.model` | `BAAI/bge-m3` | 同上 |
| `hetagen.embedding_api.dim` | `1024` | — |

---

### `hetawiki`

HetaWiki 的配置块较小，主要控制 ingest 限制，以及 default ingest、merge ingest、query、lint 所使用的 LLM。

#### `hetawiki.workspace`

| 参数 | 当前值 | 说明 |
|------|--------|------|
| `hetawiki.workspace` | `"workspace"` | `config.yaml` 中的工作区根目录设置。注意：当前 HetaWiki 的路径层仍固定使用 `workspace/hetawiki/`，因此修改该字段暂时不会真正改变工作目录。 |

#### `hetawiki.ingest`

| 参数 | 当前值 | 说明 |
|------|--------|------|
| `hetawiki.ingest.max_workers` | `4` | ingest 并发工作线程数；query 路由当前也复用这一线程池规模。 |
| `hetawiki.ingest.max_input_chars` | `80000` | `default ingest` 可接受的最大解析文本长度。超长文档会被直接拒绝，不做静默截断或自动拆页。 |
| `hetawiki.ingest.parse_timeout` | `300` | 基于 MinerU 的文档解析超时时间（秒）。大 PDF 或 OCR 较重的文件可能需要几分钟。 |

#### `hetawiki.llm`

| 参数 | 当前值 | 说明 |
|------|--------|------|
| `hetawiki.llm.base_url` | 继承自 `*dashscope` | HetaWiki 使用的 OpenAI 兼容接口地址。必填。 |
| `hetawiki.llm.api_key` | 继承自 `*dashscope` | 当前 provider 的 API Key。必填。 |
| `hetawiki.llm.model` | `"qwen3.5-flash"` | 当前用于 default ingest、merge ingest、query 和 lint 的模型。 |
| `hetawiki.llm.timeout` | `300` | 请求超时时间（秒）。merge 和长文档任务通常比普通对话请求更慢。 |
| `hetawiki.llm.max_retries` | `2` | HetaWiki LLM 请求失败后的重试次数。 |

---

## db_config.yaml

位于 `src/hetadb/config/db_config.yaml`，控制文档处理管线的吞吐量参数。默认值适用于大多数硬件配置；仅在处理超大规模数据集时需要调整。

### 顶层参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `postgres_batch_size` | `500` | PostgreSQL 批量插入的行数 |
| `parse_max_workers` | `2` | 最大并发文档解析任务数 |

### `parameter.chunk_config`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `chunk_size` | `1024` | 目标分块大小（token 数） |
| `overlap` | `50` | 相邻分块的重叠 token 数 |
| `max_workers` | `16` | 并行分块的线程池大小 |

### `parameter.graph_config`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `batch_size` | `2000` | 每批图谱抽取的分块数 |
| `max_workers` | `200` | 实体/关系抽取的最大并发 LLM 调用数 |

### `parameter.graph_merge_config`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `parallel_batches` | `16` | 并行处理的合并批次数 |
| `batch_size` | `500` | 每批合并的实体数 |

### `parameter.vector_config`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `batch_size` | `2000` | 每次 Embedding API 调用的分块数 |
| `num_threads` | `8` | Milvus 并发写入线程数 |
