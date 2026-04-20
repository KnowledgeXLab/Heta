# 查询 Wiki

在 Query 页面中，可以直接围绕当前 Wiki 提问。系统会基于已有 Wiki 内容生成答案，并给出相关来源页面。

## 适合什么场景

- 快速了解某个主题
- 比较几个相关概念
- 根据来源页面继续阅读
- 把高质量回答沉淀为可复用的综合页面

## 怎么使用

1. 打开 Query 页面。
2. 输入问题。
3. 等待答案返回。
4. 查看答案下方的来源页面标签。
5. 必要时打开来源页面或继续追问。

## 怎样提问更有效

- 一次只问一个主要问题
- 明确指出具体概念或模型
- 如果希望系统做综合分析，直接提出比较类问题

如果相关信息还不在 Wiki 中，先做 ingest。

## Archived 是什么

`archived` 表示这条回答已被保存回 Wiki，成为一篇新的综合页面。

## 给开发者的补充

提交查询：

```bash
curl -X POST http://localhost:8000/api/v1/hetawiki/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is GRPO?"}'
```

查询任务状态：

```bash
curl http://localhost:8000/api/v1/hetawiki/tasks/abc123
```
