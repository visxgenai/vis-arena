# Repository Guidelines

## Project Structure & Module Organization

This is a monorepo for the Vis Arena platform.

- `apps/server/`: FastAPI backend for auth, S3 presigned storage, Docker evaluation jobs, leaderboard, artifacts, and cloud LLM token brokerage.
- `apps/web/`: React + TypeScript + Tailwind frontend for account flows, uploads, previews, and leaderboard views.
- `apps/web/docs/`: Markdown help documents rendered in the web UI with `react-markdown`, including SDK and template-agent guidance.
- `packages/arena-sdk/`: Python SDK and `vis-arena` CLI for arena access.
- `submissions/python-template/`: Reference OpenAI participant agent bundle with `info`, `generate`, and `evaluate` commands.
- `docs/`: Protocol and contributor-facing design documentation.
- `schemas/`: JSON schemas for task, generation, and evaluation payloads.
- `examples/`: Example benchmark task bundles and data.
- `references/`: External reference projects; read for context, but do not edit unless explicitly requested.

## Build, Test, and Development Commands

Use `uv` for Python packages:

```bash
cd apps/server && uv run --with-editable . vis-arena-server
cd packages/arena-sdk && uv run --with-editable . vis-arena --help
cd submissions/python-template && uv run --with-editable ../../packages/arena-sdk --with-editable . ./agent.py info --output /tmp/agent-info.json
cd apps/server && uv run --with-editable . vis-arena-worker
```

Use `pnpm` for the frontend:

```bash
cd apps/web && pnpm install
cd apps/web && pnpm dev
cd apps/web && pnpm build
```

Run a broad Python syntax check from the repo root:

```bash
python -m compileall submissions/python-template/agent.py packages/arena-sdk/src apps/server/src
```

## Coding Style & Naming Conventions

Python targets 3.11+. Prefer typed Pydantic models for API payloads and `pathlib.Path` for paths. Keep CLIs non-interactive unless a command explicitly documents prompting. Use snake_case for Python modules/functions and kebab-case for CLI command names.

Frontend code is TypeScript with React function components and Tailwind utility classes. Use PascalCase for components, camelCase for variables/functions, and keep reusable UI structure in small typed components.

Keep user-facing arena instructions in `apps/web/docs/*.md`. When SDK commands, template-agent links, dataset format, submission commands, evaluation behavior, or authentication changes, update those Markdown docs in the same change.

## Testing Guidelines

There is no full test suite yet. Add tests beside each package as functionality grows, for example `apps/server/tests/`, `packages/arena-sdk/tests/`, and `submissions/python-template/tests/`. Prefer `pytest` for Python and Vitest/React Testing Library for frontend tests. Name Python tests `test_*.py`.

For agent behavior, keep smoke tests covering `info`, `generate`, and `evaluate` against `examples/tasks/monthly-sales`.

## Commit & Pull Request Guidelines

Use Conventional Commit-style messages, matching the existing history, for example `feat: add artifact preview endpoint` or `fix: validate unsafe zip paths`.

Pull requests should include a short summary, verification commands run, screenshots for frontend changes, and notes about schema or protocol changes. Link related issues when available and call out security-sensitive changes such as sandboxing, token brokerage, file extraction, or credential handling.

## Security & Configuration Tips

Never commit provider API keys, arena tokens, `.env` files, virtual environments, build outputs, or dependency folders. Local runs use participant-owned `OPENAI_API_KEY`. Cloud evaluation should inject `VIS_ARENA_API_TOKEN` and retrieve backend-issued provider credentials. S3 bucket credentials belong in deployment secrets only.
