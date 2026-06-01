# Vis Arena SDK

Python client and CLI for Vis Arena.

```bash
uv run --with-editable . vis-arena login user@example.com secret --server-url http://localhost:8000
uv run --with-editable . vis-arena datasets list
uv run --with-editable . vis-arena datasets upload examples/tasks/monthly-sales --name monthly-sales-v1
uv run --with-editable . vis-arena submit submission.zip --name my-agent --dataset-id <dataset-id>
```

Uploads and downloads use backend-issued presigned S3 URLs; the SDK uploads ZIP bytes directly to S3, then finalizes the record with the API.

The CLI stores the arena token in `~/.config/vis-arena/config.json` unless `VIS_ARENA_API_TOKEN` is already set.

Submitted jobs record agent runtime, phase logs, `agent-info.json`, the evaluation report, and generation/evaluation trajectories when server-side trajectory capture is enabled.
