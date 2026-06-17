# Evaluator/generation sandbox image (vis-arena-evaluator:latest).
# Pinned to Playwright v1.60.0 so its baked chromium (build 1223) matches the
# template's pinned `playwright==1.60.0` — keeping the eval's visual channel working.
# Build:  docker build -t vis-arena-evaluator:latest infra/evaluator/
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
RUN pip install --no-cache-dir --root-user-action=ignore uv
