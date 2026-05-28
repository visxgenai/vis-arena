"""Test fixtures for vis-arena server.

Sets env vars BEFORE importing the app so the module-level
`Settings` singleton picks up a throwaway SQLite path and secret.
Each test session gets a fresh DB in a tmp directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# Set env *before* importing anything that touches Settings.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="vis-arena-tests-"))
os.environ["VIS_ARENA_DB"] = str(_TMP_DIR / "test.db")
os.environ["VIS_ARENA_STORAGE"] = str(_TMP_DIR / "storage")
os.environ["VIS_ARENA_SECRET_KEY"] = "test-secret-do-not-use-in-prod"

from fastapi.testclient import TestClient  # noqa: E402

from vis_arena_server.main import app  # noqa: E402


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # Context-manager form runs FastAPI startup events (init_db).
    with TestClient(app) as c:
        yield c
