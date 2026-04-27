# Vis Arena

Vis Arena is an AI-agent arena for web data visualization tasks. Participants submit an executable agent bundle that can generate a visualization from a human-readable task and evaluate visualization artifacts using browser automation.

This repository contains:

- A protocol for task descriptions, generated artifacts, and evaluation reports.
- A Python template submission agent with `generate` and `evaluate` commands.
- A Python SDK and CLI for authentication, datasets, tasks, and submissions.
- A FastAPI backend skeleton for accounts, uploads, submissions, jobs, and cloud-only LLM token brokerage.
- A React arena frontend for browsing datasets, submissions, leaderboard entries, and visual previews.

## Repository Layout

```text
apps/server/                 FastAPI backend
apps/web/                    React frontend
docs/arena_protocol.md       Task, generation, and evaluation interfaces
examples/tasks/              Example benchmark task
packages/arena-sdk/          Python SDK and CLI
schemas/                     JSON schemas for protocol payloads
submissions/python-template/ Template participant bundle
```

## Submission Interface

Every submission bundle must expose an executable with these commands:

```bash
./agent info --output agent-info.json
./agent generate --task task.md --data-dir data --output-dir run/generated
./agent evaluate --task task.md --data-dir data --source-dir run/generated/source --built-dir run/generated/built --output run/evaluation.json
```

`generate` writes:

- `source/`: editable web source code.
- `built/`: static browser-ready artifact, usually `index.html`, CSS, and JS.
- `generation.json`: metadata about the run.

`evaluate` writes a JSON evaluation report with a score, rubric breakdown, browser observations, source observations, and reproducibility metadata.

See [docs/arena_protocol.md](docs/arena_protocol.md) for the full contract.

## Local Development

Backend:

```bash
cd apps/server
uv run --with-editable . vis-arena-server
```

SDK/CLI:

```bash
cd packages/arena-sdk
uv run --with-editable . vis-arena --help
```

Template submission:

```bash
cd submissions/python-template
uv run --with-editable ".[eval]" ./agent generate --task ../../examples/tasks/monthly-sales/task.md --data-dir ../../examples/tasks/monthly-sales/data --output-dir /tmp/vis-run
uv run --with-editable ".[eval]" ./agent evaluate --task ../../examples/tasks/monthly-sales/task.md --data-dir ../../examples/tasks/monthly-sales/data --source-dir /tmp/vis-run/source --built-dir /tmp/vis-run/built --output /tmp/vis-run/evaluation.json
```

Frontend:

```bash
cd apps/web
pnpm install
pnpm dev
```

## LLM Access Model

Participants use their own provider keys for local testing, for example `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

For cloud evaluation after submission, Vis Arena injects `VIS_ARENA_API_TOKEN` into the sandbox. The submitted agent can call the arena backend to request a short-lived LLM credential for an allowed provider/model. The backend enforces budget, model policy, and audit logging before returning a scoped token. Long-lived provider keys are never shipped in the submitted bundle.

The Python SDK includes the token helper, and the template agent documents the expected environment variables.
