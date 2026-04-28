# Connect MCP Clients

Heta exposes two MCP servers that integrate directly with any MCP-compatible client such as Claude Desktop or Cursor.

## MCP Server URLs

| Server | Port | URL |
|--------|------|-----|
| HetaMem MCP | 8011 | `http://localhost:8011/mcp/` |
| HetaDB MCP | 8012 | `http://localhost:8012/mcp/` |

## Claude Desktop

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "hetamem": { "type": "http", "url": "http://localhost:8011/mcp/" },
    "hetadb":  { "type": "http", "url": "http://localhost:8012/mcp/" }
  }
}
```

## Cursor / Other Clients

Refer to your client's MCP configuration documentation. Use the URLs above.

---

## Recommended Agent Setup

**1. Start the stack**

```bash
./scripts/bootstrap.sh
```

**2. Load the skill into your agent**

```
skills/querying-knowledge-and-memory/SKILL.md
```

See [Querying Skill](../hetamem/querying-skill.md) for details.
