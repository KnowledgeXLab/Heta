# 手动安装

不使用 Docker 运行 Heta——适合开发调试，或已有独立运行的基础设施服务时使用。

## 前提条件

- Python 3.10
- PostgreSQL（运行中）
- Milvus（运行中）
- Neo4j（运行中）

## 安装

```bash
# 1. 创建并激活环境
conda create -n heta python=3.10 -y
conda activate heta

# 2. 安装后端
pip install -e .

# 3. 构建前端
cd heta-frontend && npm install && npm run build && cd ..

# 4. 复制并填写配置
cp config.example.yaml config.yaml
# 编辑 config.yaml：填写 provider API Key
```

## 运行——统一模式

在单一端口上运行所有模块（HetaDB、HetaMem、HetaGen）：

```bash
heta serve
# → http://localhost:8000
```

`python src/main.py` 仍作为兼容入口保留，但推荐使用 `heta serve`。

## CLI 使用流

统一服务运行后：

```bash
heta status
heta insert ./docs --kb research
heta query "这个项目包含什么？" --kb research
heta remember "用户喜欢简洁示例"
```

## 运行——独立模式

各模块分别在独立端口运行：

```bash
export PYTHONPATH=/path/to/Heta/src

python src/hetadb/api/main.py              # HetaDB   → :8001
python src/hetagen/api/main.py             # HetaGen  → :8002
python src/hetamem/api/main.py             # HetaMem  → :8003

# MCP 服务器（可选）
HETAMEM_BASE_URL=http://localhost:8000 python src/hetamem/mcp/server.py  # → :8011
HETADB_BASE_URL=http://localhost:8000  python src/hetadb/mcp/server.py   # → :8012
```

## 端口参考

| 服务 | 端口 |
|------|------|
| Heta 统一 API | 8000 |
| HetaDB（独立） | 8001 |
| HetaGen（独立） | 8002 |
| HetaMem（独立） | 8003 |
| HetaMem MCP | 8011 |
| HetaDB MCP | 8012 |
| PostgreSQL | 5432 |
| Milvus | 19530 |
| Neo4j 浏览器 / Bolt | 7474 / 7687 |
| MinIO S3 / 控制台 | 9000 / 9001 |
