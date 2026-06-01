# Vis Arena SDK

Python client and CLI for Vis Arena.

```bash
uv tool install "git+https://github.com/visxgenai/vis-arena#subdirectory=packages/arena-sdk"

vis-arena init my-agent && cd my-agent
vis-arena register you@example.com 'your-password' --server-url http://44.248.40.235:8000
vis-arena datasets list
vis-arena submit . --dataset monthly-sales
```

Uploads and downloads use backend-issued presigned S3 URLs; the SDK uploads ZIP bytes directly to S3, then finalizes the record with the API.

The CLI stores the arena token in `~/.config/vis-arena/config.json` unless `VIS_ARENA_API_TOKEN` is already set.

Submitted jobs record agent runtime, phase logs, `agent-info.json`, the evaluation report, and generation/evaluation trajectories when server-side trajectory capture is enabled.
