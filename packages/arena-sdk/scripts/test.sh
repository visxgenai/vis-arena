#!/usr/bin/env bash
# Install and run the vis-arena-sdk test suite.
#
# Idempotent: creates packages/arena-sdk/.venv on first run, installs the
# backend (as a path dep, needed for in-process ASGI testing) and the SDK
# with dev extras, then runs pytest. Extra args pass through to pytest.
#
# Usage (from anywhere):
#     bash packages/arena-sdk/scripts/test.sh
#     bash packages/arena-sdk/scripts/test.sh -v -k cli

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${SDK_DIR}/../.." && pwd)"
cd "${SDK_DIR}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required but not found on PATH" >&2
  echo "       install: https://docs.astral.sh/uv/" >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "==> creating .venv"
  uv venv
fi

echo "==> installing backend (path dep, needed for in-process ASGI testing)"
uv pip install -e "${REPO_ROOT}/apps/server" --quiet

echo "==> installing SDK + dev extras"
uv pip install -e ".[dev]" --quiet

echo "==> running tests"
exec .venv/bin/pytest "$@"
