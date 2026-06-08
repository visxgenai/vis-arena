# Vis Arena Template Agent

A minimal LLM agent that builds and judges a web data visualization. It runs a
tool loop with:

- `bash(command, cwd)` — file inspection and generation
- `playwright(script, cwd)` — browser-based evaluation (the LLM writes the script, the tool executes it)
- `finish(result)` — return the generation/evaluation summary

## Files in this bundle

| File              | What it is                                                                |
|-------------------|---------------------------------------------------------------------------|
| `agent.py`        | Arena protocol entrypoint. You usually do NOT edit it.                    |
| `example_agent.py`| The example implementation (OpenAI tool-loop). **Edit or replace this.**  |
| `agent.md`        | In-bundle contract reference. Read this if you want the exact paths/JSON. |
| `submission.yaml` | Bundle metadata and command list.                                         |
| `pyproject.toml`  | Python dependencies.                                                      |

## Plug in your own agent

Edit `example_agent.py`. It exposes four hooks `agent.py` dispatches to:

```python
info()                              -> dict
models()                            -> dict   # optional, debugging
generate(workdir)                   -> dict   # write source/, dist/index.html
evaluate(workdir, artifact_url)     -> dict   # score the rendered artifact
```

Keep the names and signatures. Everything else — prompts, LLM client, tools —
is yours to delete or rewrite.

**Three integration patterns** (also documented at the top of `example_agent.py`):

1. **Import your existing Python package** — call your code from inside the hooks.
2. **Shell out to your CLI** — `subprocess.run([sys.executable, "-m", "my_agent", ...])`.
3. **Inline implementation** — replace the OpenAI tool-loop with your own code.

`workdir` for `generate` contains `task.md` and `data/`. `workdir` for
`evaluate` contains just `task.md` — the artifact is reached through
`artifact_url`, not through the workdir. See [`agent.md`](./agent.md) for
the full layout.

`artifact_url` is provided by the arena (`VIS_ARENA_ARTIFACT_URL`, pointing
at the S3-served preview) for self / peer / central-judge evaluations alike.
For local testing, `agent.py` falls back to spinning up a localhost server
pointing at `workdir/dist/`. Either way, use the URL verbatim:
`page.goto(artifact_url)` — do not reconstruct it.

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
vis-arena submit . --name "my-agent-v1"
```

`--name` is what shows on the leaderboard. Without it, the CLI uses the
directory you submit from — fine for `vis-arena init my-agent && cd my-agent`,
but if you submit from inside a generic `agent/` folder, give it a real name.
Cloud submission runs against every active public dataset on the arena server.

## Optional: test locally

Local testing uses OpenAI directly (it does not route through the arena).
Run the local preflight against a dataset. Use `--task` only for an offline
task folder or ZIP:

```bash
cat > .env <<'EOF'
OPENAI_API_KEY=sk-...
EOF

vis-arena local run . --dataset monthly-sales
```

The command reads `OPENAI_API_KEY` from `.env`, uses your stored arena server
URL to fetch the dataset, writes a local run directory under
`.vis-arena/local-runs/`, runs `info`, `generate`, then `evaluate`, and prints
the score, artifact path, and preview command.

Local evaluation uses Playwright. Install the browser once:

```bash
uv run playwright install chromium
```
