"""Test fixtures for vis-arena-sdk.

Tests run the FastAPI backend in-process via httpx.ASGITransport so
SDK and CLI tests never touch a real socket or external server.

Env vars are set at module load time, BEFORE any vis_arena_server or
vis_arena_sdk module is imported, because both packages read settings
into module-level constants (see eng-review C6).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

# --- env setup (must happen before imports below) ----------------------------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="vis-arena-sdk-tests-"))
os.environ["VIS_ARENA_DB"] = str(_TMP_ROOT / "test.db")
os.environ["VIS_ARENA_STORAGE"] = str(_TMP_ROOT / "storage")
os.environ["VIS_ARENA_SECRET_KEY"] = "test-secret-do-not-use-in-prod"
os.environ["VIS_ARENA_CONFIG_DIR"] = str(_TMP_ROOT / "sdk-config")

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from vis_arena_server.db import init_db  # noqa: E402
from vis_arena_server.main import app as fastapi_app  # noqa: E402
from vis_arena_sdk.client import VisArenaClient  # noqa: E402
from vis_arena_sdk.config import CONFIG_PATH  # noqa: E402

# Backend init runs once per test session.
init_db()

BASE_URL = "http://testserver"


@pytest.fixture(autouse=True)
def _patch_client_to_use_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every VisArenaClient route through the in-process FastAPI app.

    Starlette's TestClient is an httpx.Client subclass that bridges
    sync calls to the ASGI app via an internal portal. We swap it in
    for VisArenaClient's internal httpx.Client. Active for every test.
    """

    def patched_init(
        self: VisArenaClient,
        base_url: str = BASE_URL,
        token: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = TestClient(
            fastapi_app,
            base_url=self.base_url,
            headers=headers,
        )
        # TestClient sets its own timeout via httpx config; the SDK's
        # `timeout=` param is mostly cosmetic in test mode.

    monkeypatch.setattr(VisArenaClient, "__init__", patched_init)


@pytest.fixture
def fresh_config() -> Iterator[Path]:
    """Wipe the CLI config file before and after each test that uses it."""
    CONFIG_PATH.unlink(missing_ok=True)
    yield CONFIG_PATH
    CONFIG_PATH.unlink(missing_ok=True)


@pytest.fixture
def sdk_client() -> Iterator[VisArenaClient]:
    """A bare VisArenaClient bound to the in-process backend."""
    c = VisArenaClient(base_url=BASE_URL)
    try:
        yield c
    finally:
        c.close()
