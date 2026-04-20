# HetaWiki

HetaWiki 是 Heta 的 LLM 原生 Wiki 层。它将用户上传的原始文档编译为可版本化的 Markdown 知识库，并支持用户与智能体直接浏览、融合和查询该 Wiki。

---

## 支持格式

当前 HetaWiki 的摄入链路支持以下轻量文档格式：

| 格式 | 扩展名 |
|--------|-----------|
| 纯文本 / 标记文本 | `.md`, `.markdown`, `.txt` |
| PDF | `.pdf` |
| Word 文档 | `.docx`, `.doc` |
| 演示文稿 | `.pptx`, `.ppt` |

其中 `.doc`、`.ppt`、`.pptx` 需要先通过 LibreOffice 转为 PDF 再进行解析。

---

## 核心概念

| 组件 | 作用 |
|---|---|
| `raw/` | 用户上传后的原始文件，只进不改 |
| `wiki/pages/` | Wiki 页面正文，作为后续阅读与查询的事实来源 |
| `index.md` | 全局目录，供前端与 LLM 共同使用 |
| `log.md` | append-only 操作日志，记录 ingest、merge 与维护动作 |
| 独立 git 仓库 | 将 Wiki 内容演化与主项目代码历史分离管理 |

---

## 两种摄入模式

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `default ingest` | 将一份源文档解析并整理为一篇新的 Wiki 页面 | 快速新增文档 |
| `merge ingest` | 基于 Wiki 工作副本运行 agent loop，按需读取、更新已有页面或新增页面 | 将新文档高质量融合进现有 Wiki |

两种模式都以异步任务执行。API 会先返回 `task_id`，调用方再通过任务接口轮询结果。

---

## 查询与导航

HetaWiki 提供三类互补的访问方式：

- `POST /api/v1/hetawiki/query` — 异步问答接口
- `GET /api/v1/hetawiki/index` — 获取当前 Wiki 目录
- `GET /api/v1/hetawiki/pages/{filename}` — 读取单个页面
- `GET /api/v1/hetawiki/graph` — 将 Wiki 内部链接抽取为图谱节点与边

因此，HetaWiki 既可以作为 Web UI 功能使用，也可以作为面向智能体的可读知识层使用。

---

## 维护机制

HetaWiki 内置可配置的 lint 调度能力。Lint 会定期扫描整个 Wiki，用于发现结构不一致、缺失链接或可能过时的内容。

调度入口为 `POST /api/v1/hetawiki/lint`。

---

## 相关页面

- [文档摄入](ingest.zh.md) — 面向用户的前端上传与模式选择说明
- [查询 Wiki](query.zh.md) — 提问、查看来源页面，并继续追问
- [REST API 参考](../reference/api.md)
- [配置参考](../reference/config.md)
