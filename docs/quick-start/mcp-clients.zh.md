# 连接 MCP 客户端

Heta 提供两个 MCP 服务器，可直接集成 Claude Desktop、Cursor 等任意 MCP 兼容客户端。

## MCP 服务器地址

| 服务器 | 端口 | URL |
|--------|------|-----|
| HetaMem MCP | 8011 | `http://localhost:8011/mcp/` |
| HetaDB MCP | 8012 | `http://localhost:8012/mcp/` |

## Claude Desktop

在 `~/.claude.json` 中添加：

```json
{
  "mcpServers": {
    "hetamem": { "type": "http", "url": "http://localhost:8011/mcp/" },
    "hetadb":  { "type": "http", "url": "http://localhost:8012/mcp/" }
  }
}
```

## Cursor / 其他客户端

请参考各客户端的 MCP 配置文档，使用上方地址进行注册。

---

## 推荐的智能体配置

**1. 启动服务栈**

```bash
./scripts/bootstrap.sh
```

**2. 将技能加载给 Agent**

```
skills/querying-knowledge-and-memory/SKILL.md
```

详见[查询技能](../hetamem/querying-skill.md)。
