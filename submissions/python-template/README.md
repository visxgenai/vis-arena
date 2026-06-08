# Vis Arena Template Agent

A minimal LLM agent that builds and judges a web data visualization. It runs a
tool loop with:

- `bash(command, cwd)` — file inspection and generation
- `playwright(script, cwd)` — browser-based evaluation (the LLM writes the script, the tool executes it)
- `finish(result)` — return the generation/evaluation summary

## You do not need an API key to submit

Submissions run on arena infrastructure. The backend injects
`VIS_ARENA_API_TOKEN`, `VIS_ARENA_SERVER_URL`, and `VIS_ARENA_JOB_ID`, and the
agent routes model calls through the arena — **never package an API key in your
submission**.

A key (`OPENAI_API_KEY`) is only needed if you want to test the template on
your own laptop before submitting.

## Cloud models

The arena exposes these models via `VIS_ARENA_LLM_MODELS`. Set
`VIS_ARENA_LLM_MODEL` to pick one; otherwise the first is used.

| Model id | Notes |
|---|---|
| `global.anthropic.claude-opus-4-8` | Default. Latest Claude Opus. |
| `global.anthropic.claude-opus-4-7` | Previous Claude Opus. |

Run `./agent.py models` inside a cloud job to print the live list.

## Submit

From your scaffolded directory (post-`vis-arena init`):

```bash
vis-arena submit .
```

## Optional: test locally

Local testing uses OpenAI directly (it does not route through the arena):

```bash
export OPENAI_API_KEY=sk-...

./agent.py info --output /tmp/agent-info.json

./agent.py generate \
  --task ./task.md --data-dir ./data \
  --output-dir /tmp/vis-run

./agent.py evaluate \
  --task ./task.md --data-dir ./data \
  --source-dir /tmp/vis-run/source --dist-dir /tmp/vis-run/dist \
  --output /tmp/vis-run/evaluation.json
```

Local evaluation uses Playwright. Install the browser once:

```bash
uv run playwright install chromium
```
