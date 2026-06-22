# Evaluator/generation sandbox and AWS Batch runner image.
# Pinned to Playwright v1.60.0 so its baked chromium (build 1223) matches the
# template's pinned `playwright==1.60.0` — keeping the eval's visual channel working.
# Build from the repository root:
#   docker build -f infra/evaluator/Dockerfile -t vis-arena-evaluator-runner:latest .
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
RUN pip install --no-cache-dir --root-user-action=ignore uv
WORKDIR /opt/vis-arena
COPY apps/server ./apps/server
COPY packages/arena-sdk ./packages/arena-sdk
ENV VIS_ARENA_REPO_ROOT=/opt/vis-arena
RUN pip install --no-cache-dir --root-user-action=ignore ./apps/server
CMD ["vis-arena-runner"]
