from __future__ import annotations

import os
from pathlib import Path


class Settings:
    secret_key: str = os.environ.get("VIS_ARENA_SECRET_KEY", "dev-secret-change-me")
    database_path: Path = Path(os.environ.get("VIS_ARENA_DB", ".vis-arena/server.db"))
    storage_dir: Path = Path(os.environ.get("VIS_ARENA_STORAGE", ".vis-arena/storage"))
    public_base_url: str = os.environ.get("VIS_ARENA_PUBLIC_BASE_URL", "http://localhost:8000")
    cloud_llm_enabled: bool = os.environ.get("VIS_ARENA_CLOUD_LLM_ENABLED", "false").lower() == "true"


settings = Settings()

