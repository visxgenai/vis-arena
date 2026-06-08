"""Local filesystem storage backend for development without AWS S3.

When VIS_ARENA_LOCAL_STORAGE=true the server stores all dataset and
submission bundles under ``settings.storage_dir`` and serves them via a
FastAPI static file route instead of generating S3 presigned URLs.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .settings import settings

_root = settings.storage_dir


def local_storage_enabled() -> bool:
    import os
    return os.environ.get("VIS_ARENA_LOCAL_STORAGE", "true").lower() == "true"


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def local_presigned_put(key: str) -> dict[str, Any]:
    """Return a fake presigned-put payload pointing at the local upload endpoint."""
    return {
        "url": f"{settings.public_base_url}/_local/upload/{key}",
        "method": "PUT",
        "headers": {"Content-Type": "application/zip"},
        "expires_in": 900,
    }


def local_presigned_get(key: str) -> dict[str, Any]:
    """Return a fake presigned-get payload pointing at the local download endpoint."""
    return {
        "url": f"{settings.public_base_url}/_local/files/{key}",
        "method": "GET",
        "expires_in": 900,
    }


def local_download(key: str, target: Path) -> None:
    source = _root / key
    if not source.exists():
        raise FileNotFoundError(f"Local storage key not found: {key}")
    _ensure_dir(target)
    shutil.copy2(source, target)


def local_upload_file(source: Path, key: str) -> None:
    dest = _root / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def local_save_bytes(data: bytes, key: str) -> None:
    dest = _root / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def local_file_path(key: str) -> Path:
    return _root / key
