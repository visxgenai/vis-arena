#!/usr/bin/env bash
# Set up and run the vis-arena FastAPI backend locally.
#
# Idempotent: re-running is safe. Creates apps/server/.venv on first run,
# installs the package in editable mode with dev extras, then starts uvicorn
# in the foreground. Ctrl+C to stop.
#
# Usage (from anywhere):
#     bash apps/server/scripts/dev.sh
#     PORT=8001 bash apps/server/scripts/dev.sh   # change port

set -euo pipefail

# Resolve apps/server regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SERVER_DIR}"

PORT="${PORT:-8000}"

# --- preflight ------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required but not found on PATH" >&2
  echo "       install: https://docs.astral.sh/uv/" >&2
  exit 1
fi

if lsof -iTCP:"${PORT}" -sTCP:LISTEN -P -n >/dev/null 2>&1; then
  echo "error: port ${PORT} is already in use" >&2
  echo "       check: lsof -iTCP:${PORT} -sTCP:LISTEN" >&2
  echo "       or run with a different port: PORT=8001 bash $0" >&2
  exit 1
fi

# --- venv + deps (no-op if already up to date) ----------------------------

if [[ ! -d .venv ]]; then
  echo "==> creating .venv"
  uv venv
fi

echo "==> syncing dependencies (editable + dev extras)"
uv pip install -e ".[dev]" --quiet

# --- run ------------------------------------------------------------------

echo "==> starting server on http://localhost:${PORT}"
echo "    health:  curl http://localhost:${PORT}/health"
echo "    docs:    open http://localhost:${PORT}/docs"
echo "    stop:    Ctrl+C"
echo

# `vis-arena-server` is the entrypoint defined in pyproject.toml. It calls
# uvicorn with reload=True so file edits trigger an automatic restart.
exec env PORT="${PORT}" .venv/bin/vis-arena-server
