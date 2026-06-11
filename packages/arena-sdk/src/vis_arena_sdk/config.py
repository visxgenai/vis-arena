from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_SERVER_URL = "https://visagent.org"
CONFIG_DIR = Path(os.environ.get("VIS_ARENA_CONFIG_DIR", Path.home() / ".config" / "vis-arena"))
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def resolve_server_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("VIS_ARENA_SERVER_URL"):
        return os.environ["VIS_ARENA_SERVER_URL"].rstrip("/")
    return str(load_config().get("server_url") or DEFAULT_SERVER_URL).rstrip("/")


def resolve_token(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if os.environ.get("VIS_ARENA_API_TOKEN"):
        return os.environ["VIS_ARENA_API_TOKEN"]
    token = load_config().get("access_token")
    return str(token) if token else None
