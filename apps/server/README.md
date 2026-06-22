# Vis Arena Server

FastAPI backend for Vis Arena accounts, S3 artifact storage, submission queues,
Docker or AWS Batch evaluation, and cloud-only OpenAI token brokerage.

```bash
cp .env.example .env
uv run --with-editable . vis-arena-server
uv run --with-editable . vis-arena-worker
```

The server loads `.env` and `.env.local` from `apps/server/` or the current
working directory. Real environment variables take precedence over file values.

Required storage settings:

- `VIS_ARENA_S3_BUCKET`
- `VIS_ARENA_S3_REGION`
- `VIS_ARENA_S3_ENDPOINT_URL` for MinIO or non-AWS S3-compatible storage

Worker settings:

- `VIS_ARENA_EXECUTOR_MODE`, `local_docker` by default or `aws_batch_fargate`
- `VIS_ARENA_EVALUATOR_IMAGE`, default `mcr.microsoft.com/playwright/python:v1.60.0-noble`
- `VIS_ARENA_WORKER_API_TOKEN`, injected into Docker jobs as `VIS_ARENA_API_TOKEN`
- `VIS_ARENA_CLOUD_LLM_ENABLED=true`
- `VIS_ARENA_BROKERED_OPENAI_API_KEY` for brokered OpenAI access

AWS Batch settings, used only when `VIS_ARENA_EXECUTOR_MODE=aws_batch_fargate`:

- `VIS_ARENA_AWS_BATCH_REGION`
- `VIS_ARENA_AWS_BATCH_JOB_QUEUE`
- `VIS_ARENA_AWS_BATCH_JOB_DEFINITION`
- `VIS_ARENA_AWS_BATCH_RUNNER_IMAGE`, optional image override for the Batch job

SQLite remains the metadata database. Dataset bundles, submission bundles, and
job artifact ZIPs are stored in S3 using presigned upload/download URLs.
