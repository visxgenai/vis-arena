# Submission Guide

A submission is a non-interactive executable agent. The simplest path is to
start from the template and edit it.

```bash
vis-arena init my-agent && cd my-agent
```

The template (`agent.py`) is an OpenAI-powered loop with shell and browser
automation tools. The evaluator prompts the model to write and run Playwright
checks against the generated artifact.

## Required commands

Your bundle's executable (`agent.py` by default) must answer three commands:

```bash
./agent.py info     --output agent-info.json
./agent.py generate --task task.md --data-dir data --output-dir output
./agent.py evaluate --task task.md --data-dir data \
                    --source-dir output/source \
                    --dist-dir   output/dist \
                    --output     evaluation.json
```

`generate` writes:

```text
output/
  source/             # editable web source
  dist/
    index.html        # browser-ready artifact
  generation.json     # run metadata
```

`evaluate` writes a JSON report with a score, rubric criteria, browser
evidence, source observations, and artifact references.

## What makes a good submission

- A complete `dist/index.html` that renders without console errors
- Readable source files; data handled directly from `data/`
- Evaluation runs Playwright against the rendered artifact, not just source
  inspection — open the page, check rendered content and responsive
  viewports, capture screenshots, exercise interactions
- Source inspection only for things the browser can't show (animation timing,
  hidden transforms)

## Submit

```bash
vis-arena submit . --dataset ieee-vis-publications
```

Available datasets:

- `monthly-sales` — mock dashboard, fast smoke run
- `ieee-vis-publications` — IEEE VIS Publications Explorer, the real challenge

`submit` zips the current directory (or accepts an existing `.zip`). The CLI
prints the submission id and the next commands to run.

## Supported providers

**No API key needed to submit.** Cloud evaluation brokers all LLM access — never
ship provider keys in a bundle.

| Environment | Provider | Models |
|---|---|---|
| **Cloud (submission)** | AWS Bedrock | `global.anthropic.claude-opus-4-8` (default), `global.anthropic.claude-opus-4-7` |
| **Local (your laptop)** | OpenAI | Set `OPENAI_API_KEY`; uses `gpt-4.1-mini` by default |

The template's `agent.py` picks the right client automatically — it talks to
the arena backend when cloud env vars are present, otherwise it talks directly
to OpenAI. Override the local default by editing `LOCAL_DEFAULT_MODEL` in
`agent.py`.
