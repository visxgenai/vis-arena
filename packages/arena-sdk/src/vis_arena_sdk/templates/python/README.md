# Python OpenAI Template Submission

This template is a real, minimal LLM agent. It runs an OpenAI tool loop with:

- `bash(command, cwd)` for file inspection and generation.
- `playwright(script, cwd)` for browser-based evaluation. The LLM writes the Playwright script and the tool executes it.
- `finish(result)` to return generation/evaluation summaries.

Commands:

```bash
uv run --with-editable ../../packages/arena-sdk --with-editable . \
  ./agent.py info --output /tmp/agent-info.json

uv run --with-editable ../../packages/arena-sdk --with-editable . \
  ./agent.py models

OPENAI_API_KEY=... uv run --with-editable ../../packages/arena-sdk --with-editable . \
  ./agent.py generate --task ../../examples/tasks/monthly-sales/task.md \
  --data-dir ../../examples/tasks/monthly-sales/data --output-dir /tmp/vis-run

OPENAI_API_KEY=... uv run --with-editable ../../packages/arena-sdk --with-editable . \
  ./agent.py evaluate --task ../../examples/tasks/monthly-sales/task.md \
  --data-dir ../../examples/tasks/monthly-sales/data \
  --source-dir /tmp/vis-run/source --dist-dir /tmp/vis-run/dist \
  --output /tmp/vis-run/evaluation.json
```

Local tests use your own `OPENAI_API_KEY`. During cloud evaluation, the backend injects
`VIS_ARENA_API_TOKEN`, `VIS_ARENA_SERVER_URL`, and `VIS_ARENA_JOB_ID`; the agent routes
model calls through the arena backend so provider keys stay on the server and token usage
can be tracked per submission. Do not package provider keys in submissions.

Cloud deployments may expose multiple models through `VIS_ARENA_LLM_MODELS`. Set
`VIS_ARENA_LLM_MODEL` to choose one; otherwise the first configured model is used.

If local evaluation uses Playwright, install browsers once with:

```bash
uv run --with-editable . playwright install chromium
```
