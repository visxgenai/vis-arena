from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_env() -> None:
    app_dir = Path(__file__).resolve().parents[2]
    candidates = [
        Path.cwd() / ".env",
        Path.cwd() / ".env.local",
        app_dir / ".env",
        app_dir / ".env.local",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        _load_env_file(resolved)


load_env()


class Settings:
    secret_key: str = os.environ.get("VIS_ARENA_SECRET_KEY", "dev-secret-change-me")
    database_path: Path = Path(os.environ.get("VIS_ARENA_DB", ".vis-arena/server.db"))
    storage_dir: Path = Path(os.environ.get("VIS_ARENA_STORAGE", ".vis-arena/storage"))
    public_base_url: str = os.environ.get("VIS_ARENA_PUBLIC_BASE_URL", "http://localhost:8000")
    cloud_llm_enabled: bool = os.environ.get("VIS_ARENA_CLOUD_LLM_ENABLED", "false").lower() == "true"
    llm_provider: str = os.environ.get("VIS_ARENA_LLM_PROVIDER", "openai")
    brokered_openai_api_key: str | None = os.environ.get("VIS_ARENA_BROKERED_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    bedrock_region: str = os.environ.get("VIS_ARENA_BEDROCK_REGION", "us-west-2")
    bedrock_model_ids: list[str] = [
        model.strip()
        for model in os.environ.get("VIS_ARENA_BEDROCK_MODEL_IDS", "global.anthropic.claude-opus-4-8,global.anthropic.claude-opus-4-7").split(",")
        if model.strip()
    ]
    bedrock_default_model_id: str = bedrock_model_ids[0] if bedrock_model_ids else ""
    llm_max_tokens_per_submission: int = int(os.environ.get("VIS_ARENA_LLM_MAX_TOKENS_PER_SUBMISSION", "1000000"))
    llm_input_usd_per_1m: float = float(os.environ.get("VIS_ARENA_LLM_INPUT_USD_PER_1M", "0"))
    llm_output_usd_per_1m: float = float(os.environ.get("VIS_ARENA_LLM_OUTPUT_USD_PER_1M", "0"))
    s3_bucket: str = os.environ.get("VIS_ARENA_S3_BUCKET", "vis-arena-dev")
    s3_region: str = os.environ.get("VIS_ARENA_S3_REGION", "us-east-1")
    s3_endpoint_url: str | None = os.environ.get("VIS_ARENA_S3_ENDPOINT_URL") or None
    presign_ttl_seconds: int = int(os.environ.get("VIS_ARENA_PRESIGN_TTL_SECONDS", "900"))
    evaluator_image: str = os.environ.get("VIS_ARENA_EVALUATOR_IMAGE", "mcr.microsoft.com/playwright/python:v1.52.0-noble")
    evaluator_network: str = os.environ.get("VIS_ARENA_EVALUATOR_NETWORK", "bridge")
    evaluator_timeout_seconds: int = int(os.environ.get("VIS_ARENA_EVALUATOR_TIMEOUT_SECONDS", "1800"))
    record_trajectory: bool = os.environ.get("VIS_ARENA_RECORD_TRAJECTORY", "true").lower() == "true"
    arena_api_token: str | None = os.environ.get("VIS_ARENA_WORKER_API_TOKEN")
    rounds_enabled: bool = os.environ.get("VIS_ARENA_ROUNDS_ENABLED", "false").lower() == "true"
    round_interval_seconds: int = int(os.environ.get("VIS_ARENA_ROUND_INTERVAL_SECONDS", "3600"))
    auto_start_peer_review: bool = os.environ.get("VIS_ARENA_AUTO_START_PEER_REVIEW", "false").lower() == "true"
    central_judge_submission_id: str | None = os.environ.get("VIS_ARENA_CENTRAL_JUDGE_SUBMISSION_ID") or None


settings = Settings()
