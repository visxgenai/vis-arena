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

A fresh workdir for the evaluate phase:

```
workdir/
  task.md           # in
  data/             # in
  dist/             # in — copied from generate's dist/
    index.html
  evaluation.json   # OUT — the scoring report, written automatically by agent.py
```

`source/` is intentionally NOT staged here — evaluation interacts with the
rendered artifact, not the source code.

### Artifact URL

`agent.py` starts a localhost HTTP server bound to `workdir/dist/` and passes
the URL to your hook:

```python
def evaluate(workdir: Path, artifact_url: str) -> dict:
    # artifact_url looks like: http://127.0.0.1:38192/index.html
    # Open it with Playwright; do NOT read dist/index.html as a file.
```

Playwright against this URL behaves the same way the arena's preview endpoint
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
