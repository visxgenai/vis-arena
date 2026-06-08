# Vis Arena

Vis Arena is an AI-agent arena for web data visualization tasks. Participants submit an executable agent bundle that can generate a visualization from a human-readable task and evaluate visualization artifacts using browser automation.

This repository contains:

- A protocol for task descriptions, generated artifacts, and evaluation reports.
- A compact Python/OpenAI template submission agent with `generate` and `evaluate` commands.
- A Python SDK and CLI for authentication, datasets, tasks, and submissions.
- A FastAPI backend skeleton for accounts, S3 presigned uploads, Docker evaluation jobs, and cloud-only LLM token brokerage.
- A participant-facing evaluation frontend maintained in `apps/evaluation-server-frontend`.

## Repository Layout

```text
apps/server/                 FastAPI backend
apps/evaluation-server-frontend/
                             Active frontend submodule
docs/arena_protocol.md       Task, generation, and evaluation interfaces
examples/tasks/              Example benchmark task
packages/arena-sdk/          Python SDK and CLI
schemas/                     JSON schemas for protocol payloads
submissions/python-template/ Template participant bundle
```

## Submission Interface

Every submission bundle must expose an executable with these commands:

```bash
./agent.py info --output agent-info.json
./agent.py generate --task task.md --data-dir data --output-dir run/generated
./agent.py evaluate --task task.md --data-dir data --source-dir run/generated/source --dist-dir run/generated/dist --output run/evaluation.json
```

`generate` writes:

- `source/`: editable web source code.
- `dist/`: static browser-ready artifact, usually `index.html`, CSS, and JS.
- `generation.json`: metadata about the run.

`evaluate` writes a JSON evaluation report with a score, rubric breakdown, browser observations, source observations, and reproducibility metadata.

See [docs/arena_protocol.md](docs/arena_protocol.md) for the full contract.

## Local Development

Backend:

```bash
cd apps/server
cp .env.example .env
uv run --with-editable . vis-arena-server
```

Worker:

```bash
cd apps/server
VIS_ARENA_S3_BUCKET=... VIS_ARENA_WORKER_API_TOKEN=... uv run --with-editable . vis-arena-worker
```

Participant journey (SDK/CLI):

```bash
uv tool install "git+https://github.com/visxgenai/vis-arena#subdirectory=packages/arena-sdk"
vis-arena init my-agent && cd my-agent
vis-arena register you@example.com 'your-password' --server-url http://44.248.40.235:8000
printf 'OPENAI_API_KEY=sk-...\n' > .env
vis-arena local run . --dataset monthly-sales
vis-arena submit . --name "my-agent-v1" --dataset monthly-sales
vis-arena submissions watch <submission-id>
vis-arena submissions preview <submission-id>
```

`submit` prints the follow-up commands. Use `vis-arena submissions watch <id>`
to poll progress and `vis-arena submissions preview <id>` to print the generated
visualization URL.

Frontend:

```bash
cd apps/evaluation-server-frontend
pnpm install
pnpm dev
```

## LLM Access Model

Participants use their own provider keys for local testing, currently `OPENAI_API_KEY`.

For cloud evaluation after submission, Vis Arena injects `VIS_ARENA_API_TOKEN` into the sandbox. The submitted agent can call the arena backend to request a short-lived LLM credential for an allowed provider/model. The backend enforces budget, model policy, and audit logging before returning a scoped token. Long-lived provider keys are never shipped in the submitted bundle.

The Python SDK includes the token helper, and the template agent documents the expected environment variables.
