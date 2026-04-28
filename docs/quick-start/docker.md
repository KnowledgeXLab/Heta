# Bootstrap with Docker

The recommended way to run Heta. `bootstrap.sh` brings up the full stack — API, web UI, PostgreSQL, Milvus, Neo4j, and MinIO — with one command.

The script first pulls published GHCR images. If those images are unavailable, it automatically falls back to building backend and frontend images from local source.

## Prerequisites

- Docker ≥ 24.0
- Docker Compose ≥ 2.20
- At least one LLM provider account

## 1. Clone and copy config

=== "Default"

    ```bash
    git clone https://github.com/KnowledgeXLab/Heta.git
    cd Heta
    cp config.example.yaml config.yaml
    ```

=== "zh"

    ```bash
    git clone https://github.com/KnowledgeXLab/Heta.git
    cd Heta
    cp config.example.zh.yaml config.yaml
    ```

## 2. Fill in API keys

The `providers` block in `config.yaml` defines model provider credentials. Other infrastructure settings are preconfigured for Docker Compose.

=== "Default"

    Fill in the provider keys required by `config.example.yaml`:

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

    The CLI judge uses `heta_cli.judge`. By default, this template points it to Gemini.

=== "zh"

    `config.example.zh.yaml` already points to DashScope and SiliconFlow. Just fill in your API keys:

    ```yaml
    providers:
      dashscope:
        api_key: "YOUR_DASHSCOPE_API_KEY"   # https://dashscope.aliyun.com
      siliconflow:
        api_key: "YOUR_SILICONFLOW_API_KEY" # https://siliconflow.cn
    ```


## 3. Start

```bash
./scripts/bootstrap.sh
```

Useful options:

```bash
./scripts/bootstrap.sh --no-open  # do not open the browser
./scripts/bootstrap.sh --build    # skip GHCR pull and build locally
```

## 4. Verify

```bash
heta status
```

## Service URLs

| URL | Description |
|-----|-------------|
| http://localhost | Heta web UI |
| http://localhost:8000/docs | REST API (Swagger) |
| http://localhost:7474 | Neo4j browser |
| http://localhost:9001 | MinIO console |

## CLI workflow

```bash
heta insert ./docs --kb research
heta query "What does this project contain?" --kb research
heta remember "The user prefers concise examples"
heta status
```

`heta insert` uploads supported files into HetaDB and follows parsing progress by default. Use `-b` / `--background` to start parsing and return immediately.

## Stop

```bash
docker-compose down         # stop, keep data
docker-compose down -v      # stop and delete all volumes
```
