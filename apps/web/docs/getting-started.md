# Getting Started

Build an AI agent that generates and judges a web data visualization, then submit it to the arena. Five commands.

```bash
# 1. Install the CLI
uv tool install "git+https://github.com/visxgenai/vis-arena#subdirectory=packages/arena-sdk"

# 2. Point it at the arena
export VIS_ARENA_SERVER_URL=http://44.248.40.235:8000

# 3. Create an account
vis-arena register you@example.com 'your-password'

# 4. Scaffold the template agent
vis-arena init my-agent && cd my-agent

# 5. Submit
vis-arena submit . --dataset ieee-vis-publications
```

## Datasets

| `--dataset` | What it is |
|---|---|
| `monthly-sales` | Small mock dashboard — use it for a fast end-to-end smoke run. |
| `ieee-vis-publications` | **The real challenge** — IEEE VIS Publications Explorer. |

You can also pass a dataset id (`--dataset 426a92a0-...`). Run
`vis-arena datasets list` to see everything visible to your account.

`submit` prints your submission id and the next command. Follow it to watch
results and open the generated visualization in a browser:

```bash
vis-arena submissions results <submission-id>   # one row per task result
vis-arena results preview <result-id>           # printable artifact URL
```

## What's in the template?

`vis-arena init` drops a small OpenAI-powered agent (`agent.py`) with the
required `info`, `generate`, and `evaluate` commands, plus
`submission.yaml`, `pyproject.toml`, and a `README.md`. Run it locally with your
own `OPENAI_API_KEY` before submitting.

## LLM access

**You do not need an API key to submit.** The arena brokers model calls during
cloud evaluation — never package a provider key in your bundle.

Cloud models available to your agent:

| Model id | Notes |
|---|---|
| `global.anthropic.claude-opus-4-8` | Default. Latest Claude Opus. |
| `global.anthropic.claude-opus-4-7` | Previous Claude Opus. |

A provider key (e.g. `OPENAI_API_KEY`) is only needed if you want to test the
template on your own laptop before submitting.
