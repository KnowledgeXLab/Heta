# 文档摄入

使用 Ingest 可以把文档加入当前 Wiki。

## 两种模式

- **Add as New Page**：把上传文档生成为一篇新的 Wiki 页面。
- **Integrate with Wiki**：把上传文档融合进当前 Wiki，并在需要时更新相关页面。

## 什么时候用哪种模式

以下情况使用 **Add as New Page**：

- 文档是新的
- 希望一份文档对应一篇页面
- 不希望已有页面被修改

以下情况使用 **Integrate with Wiki**：

- 文档与现有 Wiki 主题重合
- 现有页面可能需要更新
- 希望结果被整合进当前 Wiki，而不是简单新增

## 怎么使用

1. 打开 HetaWiki 页面。
2. 在 Ingest 区域上传文件。
3. 选择模式。
4. 提交任务。
5. 等待任务完成。
6. 在页面视图、目录、图谱或查询页中检查结果。

## 说明

- 如果拿不准，优先使用 **Add as New Page**。
- 超长文档可能会被 `default ingest` 直接拒绝。
- **Integrate with Wiki** 更慢，因为它会更仔细地更新 Wiki。

## API

提交 ingest 任务：

```bash
curl -X POST http://localhost:8000/api/v1/hetawiki/ingest \
  -F "file=@deepseek-r1.pdf" \
  -F "merge=false"
```

查询任务状态：

```bash
curl http://localhost:8000/api/v1/hetawiki/tasks/abc123
```
