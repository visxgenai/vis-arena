from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException, UploadFile

from .db import connect, now_iso
from .settings import settings


def save_upload(upload: UploadFile, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle)


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            destination = (target_dir / member.filename).resolve()
            if not str(destination).startswith(str(target_dir.resolve())):
                raise HTTPException(status_code=400, detail="Unsafe ZIP path")
        archive.extractall(target_dir)


def create_dataset(owner_id: str, name: str, visibility: str, upload: UploadFile) -> dict[str, Any]:
    dataset_id = str(uuid.uuid4())
    root = settings.storage_dir / "datasets" / dataset_id
    bundle_path = root / "bundle.zip"
    extract_dir = root / "extracted"
    save_upload(upload, bundle_path)
    safe_extract_zip(bundle_path, extract_dir)
    tasks = _discover_tasks(extract_dir)
    with connect() as db:
        db.execute(
            "insert into datasets (id, owner_id, name, visibility, task_count, storage_path, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (dataset_id, owner_id, name, visibility, len(tasks), str(root), now_iso()),
        )
        for task in tasks:
            db.execute(
                "insert into tasks (id, dataset_id, title, version, metadata_json, task_path) values (?, ?, ?, ?, ?, ?)",
                (
                    task["id"],
                    dataset_id,
                    task["title"],
                    int(task.get("version") or 1),
                    json.dumps(task["metadata"]),
                    str(task["path"]),
                ),
            )
    return {"id": dataset_id, "name": name, "visibility": visibility, "task_count": len(tasks), "created_at": now_iso()}


def create_submission(owner_id: str, name: str, upload: UploadFile) -> dict[str, Any]:
    submission_id = str(uuid.uuid4())
    root = settings.storage_dir / "submissions" / submission_id
    bundle_path = root / "submission.zip"
    extract_dir = root / "extracted"
    save_upload(upload, bundle_path)
    safe_extract_zip(bundle_path, extract_dir)
    if not ((extract_dir / "agent").exists() or (extract_dir / "agent.py").exists()):
        raise HTTPException(status_code=400, detail="Submission must contain agent or agent.py")
    created_at = now_iso()
    with connect() as db:
        db.execute(
            "insert into submissions (id, owner_id, name, status, score, storage_path, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (submission_id, owner_id, name, "queued", None, str(root), created_at),
        )
    return {"id": submission_id, "name": name, "status": "queued", "score": None, "created_at": created_at}


def _discover_tasks(root: Path) -> list[dict[str, Any]]:
    tasks = []
    for path in root.rglob("task.md"):
        text = path.read_text(encoding="utf-8")
        metadata = {}
        if text.startswith("---\n"):
            _, frontmatter, _body = text.split("---", 2)
            metadata = yaml.safe_load(frontmatter) or {}
        task_id = str(metadata.get("id") or path.parent.name)
        tasks.append({
            "id": task_id,
            "title": str(metadata.get("title") or task_id),
            "version": int(metadata.get("version") or 1),
            "metadata": metadata,
            "path": path,
        })
    if not tasks:
        raise HTTPException(status_code=400, detail="Dataset bundle must include at least one task.md")
    return tasks

