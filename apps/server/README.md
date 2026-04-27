# Vis Arena Server

FastAPI backend skeleton for Vis Arena.

```bash
uv run --with-editable . vis-arena-server
```

Default storage is local:

- SQLite database: `.vis-arena/server.db`
- Uploaded files: `.vis-arena/storage`

Production deployments should replace local job execution with isolated container workers and configure provider-specific LLM token brokerage.
