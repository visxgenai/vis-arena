# Agent contract

Your bundle ships an executable `agent.py`. The arena worker invokes it three
ways:

```
./agent.py info     --output agent-info.json
./agent.py generate <workdir>
./agent.py evaluate <workdir>
```

`info` is metadata only. Generate and evaluate each receive a **single working
directory** that holds everything they need at fixed relative paths.

## Generate

The worker stages this for you; you write the rest:

```
workdir/
  task.md           # in  — Markdown task with YAML frontmatter
  data/             # in  — task data files
  source/           # OUT — editable web source (yours)
  dist/             # OUT — deployable artifact (yours)
    index.html      #       required, must work without a dev server
  generation.json   # OUT — slim metadata, written automatically by agent.py
```

You only need to produce `source/` and `dist/index.html`. `agent.py` writes
`generation.json` for you from the return value of your `generate(workdir)`
hook.

## Evaluate

A minimal workdir for the evaluate phase:

```
workdir/
  task.md           # in   — what was asked
  evaluation.json   # OUT  — the scoring report, written automatically by agent.py
```

That's it. The evaluator does NOT receive the artifact's source, dist, or
data files. Evaluation interacts with the rendered artifact entirely through
`ARTIFACT_URL` (below).

This is the **same shape for every kind of eval** — self, peer, and central
judge. Your hook can't tell them apart, which is the point: identical code
path everywhere.

### Artifact URL

`agent.py` resolves `artifact_url` one of two ways:

1. **Arena evaluation** (self / peer / central judge): the worker sets
   `VIS_ARENA_ARTIFACT_URL` pointing at the artifact's S3-served preview, e.g.
   `https://<server>/v1/jobs/<artifact-job-id>/preview/index.html`. `agent.py`
   passes it straight to your hook.

2. **Local testing** (no env var): `agent.py` falls back to spinning up a
   localhost HTTP server pointing at `workdir/dist/index.html` (you put it
   there by running `generate` first). The URL looks like
   `http://127.0.0.1:38192/index.html` with a dynamic port.

Either way your hook signature is the same:

```python
def evaluate(workdir: Path, artifact_url: str) -> dict:
    # artifact_url is a complete, ready-to-use URL — pass it through verbatim.
    # Do NOT reconstruct it; do NOT hardcode localhost:8080.
    await page.goto(artifact_url)
```

Playwright against the URL behaves the same way the arena's preview endpoint
will when reviewers view your artifact later (proper origin, `fetch()` works,
ES modules import correctly).

## What your `evaluation.json` should contain

```json
{
  "score": 72.5,
  "max_score": 100,
  "summary": "Readable bar chart with complete data but limited interaction.",
  "criteria": [
    {"id": "correctness", "score": 28, "max_score": 35, "evidence": ["..."]}
  ],
  "browser": {
    "tool": "playwright",
    "entrypoint_url": "...",
    "viewports": [{"width": 1440, "height": 900, "checks": ["..."], "screenshot": "..."}]
  },
  "artifacts": {"screenshots": ["..."], "logs": []},
  "metadata": {"evaluator": "your-agent-name"}
}
```

`agent.py` adds `schema_version`, `task_id`, and `metadata.evaluated_at`
automatically.

## Where to plug in your own agent

Edit `example_agent.py` — it defines the four hooks `agent.py` dispatches to.
Three common integration patterns are documented in the docstring at the top of
that file: import an existing Python package, shell out to your CLI, or write
the agent inline.
