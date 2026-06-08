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

`workdir` contains `task.md`, `data/`, and (during evaluate) `dist/index.html`
at fixed relative paths. See [`agent.md`](./agent.md) for the full layout.

`artifact_url` (evaluate only) is a localhost HTTP URL that serves
`workdir/dist/`. Open it with Playwright via `page.goto(artifact_url)` —
do not read `dist/index.html` as a file.

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
vis-arena submit . --dataset ieee-vis-publications
```

## Optional: test locally

Local testing uses OpenAI directly (it does not route through the arena).
Stage a workdir that contains a task and its data, then run the two phases:

```bash
export OPENAI_API_KEY=sk-...

# 1. Build a workdir from an example task.
mkdir -p /tmp/vis-run
cp -r ./task.md ./data /tmp/vis-run/

# 2. Run the protocol.
./agent.py info --output /tmp/agent-info.json
./agent.py generate /tmp/vis-run        # writes source/, dist/, generation.json
./agent.py evaluate /tmp/vis-run        # writes evaluation.json
```

Local evaluation uses Playwright. Install the browser once:

```bash
uv run playwright install chromium
```
