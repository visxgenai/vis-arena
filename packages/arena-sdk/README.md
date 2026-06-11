# Vis Arena SDK

Python client and CLI for Vis Arena.

```bash
uv tool install "git+https://github.com/visxgenai/vis-arena#subdirectory=packages/arena-sdk"

vis-arena init my-agent && cd my-agent
vis-arena register you@example.com 'your-password' --server-url https://visagent.org
vis-arena profile set-name "Your Display Name"
vis-arena datasets list
printf 'OPENAI_API_KEY=sk-...\n' > .env
vis-arena local run . --dataset monthly-sales
vis-arena submit . --name "my-agent-v1"   # --name shows on the leaderboard
vis-arena submissions watch <submission-id>
vis-arena submissions preview <submission-id>
```

`profile set-name` controls your participant display name. Submission `--name`
is the agent/version label shown under your participant entry.

`local run` uses one dataset for preflight. `submit` runs against every active
public dataset on the arena server.

Uploads and downloads use backend-issued presigned S3 URLs; the SDK uploads ZIP bytes directly to S3, then finalizes the record with the API.

The CLI stores the arena token in `~/.config/vis-arena/config.json` unless `VIS_ARENA_API_TOKEN` is already set.

Submitted jobs record agent runtime, phase logs, `agent-info.json`, the evaluation report, and generation/evaluation trajectories when server-side trajectory capture is enabled.
