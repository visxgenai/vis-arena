# Python OpenAI Template Submission

This template is a real, minimal LLM agent. It runs an OpenAI tool loop with:

- `bash(command, cwd)` for file inspection and generation.
- `playwright(script, cwd)` for browser-based evaluation. The LLM writes the Playwright script and the tool executes it.
- `finish(result)` to return generation/evaluation summaries.

Commands:

```bash
uv run --with-editable ../../packages/arena-sdk --with-editable . \
  ./agent.py info --output /tmp/agent-info.json

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
`VIS_ARENA_API_TOKEN` and `VIS_ARENA_SERVER_URL`; the agent asks `vis-arena-sdk` for a
short-lived OpenAI-compatible token/proxy. Do not package provider keys in submissions.

If local evaluation uses Playwright, install browsers once with:

```bash
uv run --with-editable . playwright install chromium
```
