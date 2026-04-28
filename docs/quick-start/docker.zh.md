# Docker 自动部署

推荐的 Heta 运行方式。`bootstrap.sh` 一条命令即可启动完整服务栈——API、Web UI、PostgreSQL、Milvus、Neo4j 和 MinIO。

脚本会优先拉取已发布的 GHCR 镜像；如果镜像不可用，会自动回退到本地源码构建。

## 前提条件

- Docker ≥ 24.0
- Docker Compose ≥ 2.20
- 至少一个 LLM 服务商账号

## 1. 克隆并复制配置

=== "zh（DashScope + SiliconFlow）"

    ```bash
    git clone https://github.com/KnowledgeXLab/Heta.git
    cd Heta
    cp config.example.zh.yaml config.yaml
    ```

=== "默认模板"

    ```bash
    git clone https://github.com/KnowledgeXLab/Heta.git
    cd Heta
    cp config.example.yaml config.yaml
    ```

## 2. 填入 API Key

`config.yaml` 的 `providers` 块定义模型服务商凭据。Docker Compose 所需的数据库和对象存储地址已预配置，通常无需修改。

=== "zh（DashScope + SiliconFlow）"

    `config.example.zh.yaml` 已指向 DashScope 和 SiliconFlow，并配置好对应模型。只需填入 API Key：

    ```yaml
    providers:
      dashscope:
        api_key: "YOUR_DASHSCOPE_API_KEY"   # https://dashscope.aliyun.com
      siliconflow:
        api_key: "YOUR_SILICONFLOW_API_KEY" # https://siliconflow.cn
    ```

=== "默认模板"

    按 `config.example.yaml` 填入所需服务商 API Key：

    ```yaml
    providers:
      dashscope:
        api_key: "YOUR_DASHSCOPE_API_KEY"
      siliconflow:
        api_key: "YOUR_SILICONFLOW_API_KEY"
      openai:
        api_key: "YOUR_OPENAI_API_KEY"
      gemini:
        api_key: "YOUR_GEMINI_API_KEY"
    ```

    CLI 判断模型使用 `heta_cli.judge`。默认模板指向 Gemini。


## 3. 启动

```bash
./scripts/bootstrap.sh
```

常用参数：

```bash
./scripts/bootstrap.sh --no-open  # 不自动打开浏览器
./scripts/bootstrap.sh --build    # 跳过 GHCR 拉取，直接本地构建
```

## 4. 验证

```bash
heta status
```

## 服务地址

| 地址 | 说明 |
|------|------|
| http://localhost | Heta Web UI |
| http://localhost:8000/docs | REST API（Swagger） |
| http://localhost:7474 | Neo4j 浏览器 |
| http://localhost:9001 | MinIO 控制台 |

## CLI 使用流

```bash
heta insert ./docs --kb research
heta query "这个项目包含什么？" --kb research
heta remember "用户喜欢简洁示例"
heta status
```

`heta insert` 会把支持的文件上传到 HetaDB，并默认跟踪解析进度。使用 `-b` / `--background` 可让解析在后台继续执行。

## 停止

```bash
docker-compose down         # 停止，保留数据
docker-compose down -v      # 停止并删除所有数据卷
```
