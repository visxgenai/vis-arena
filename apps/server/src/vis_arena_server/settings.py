from __future__ import annotations

import os
from pathlib import Path


class Settings:
    secret_key: str = os.environ.get("VIS_ARENA_SECRET_KEY", "dev-secret-change-me")
    database_path: Path = Path(os.environ.get("VIS_ARENA_DB", ".vis-arena/server.db"))
    storage_dir: Path = Path(os.environ.get("VIS_ARENA_STORAGE", ".vis-arena/storage"))
    public_base_url: str = os.environ.get("VIS_ARENA_PUBLIC_BASE_URL", "http://localhost:8000")
    cloud_llm_enabled: bool = os.environ.get("VIS_ARENA_CLOUD_LLM_ENABLED", "false").lower() == "true"
    brokered_openai_api_key: str | None = os.environ.get("VIS_ARENA_BROKERED_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    s3_bucket: str = os.environ.get("VIS_ARENA_S3_BUCKET", "vis-arena-dev")
    s3_region: str = os.environ.get("VIS_ARENA_S3_REGION", "us-east-1")
    s3_endpoint_url: str | None = os.environ.get("VIS_ARENA_S3_ENDPOINT_URL") or None
    presign_ttl_seconds: int = int(os.environ.get("VIS_ARENA_PRESIGN_TTL_SECONDS", "900"))
    evaluator_image: str = os.environ.get("VIS_ARENA_EVALUATOR_IMAGE", "mcr.microsoft.com/playwright/python:v1.52.0-noble")
    evaluator_network: str = os.environ.get("VIS_ARENA_EVALUATOR_NETWORK", "bridge")
    evaluator_timeout_seconds: int = int(os.environ.get("VIS_ARENA_EVALUATOR_TIMEOUT_SECONDS", "1800"))
    arena_api_token: str | None = os.environ.get("VIS_ARENA_WORKER_API_TOKEN")


settings = Settings()
