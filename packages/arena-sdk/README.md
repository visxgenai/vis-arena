# Vis Arena SDK

Python client and CLI for Vis Arena.

```bash
uv run --with-editable . vis-arena login --email user@example.com --password secret --server-url http://localhost:8000
uv run --with-editable . vis-arena datasets list
uv run --with-editable . vis-arena datasets upload examples/tasks/monthly-sales --name monthly-sales-v1
uv run --with-editable . vis-arena submissions upload submission.zip --name my-agent
```

The CLI stores the arena token in `~/.config/vis-arena/config.json` unless `VIS_ARENA_API_TOKEN` is already set.
