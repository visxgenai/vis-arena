from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.exceptions import ClientError
from botocore.client import Config
from fastapi import HTTPException

from .db import connect, now_iso
from .settings import settings


def s3_client():
    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        config=Config(signature_version="s3v4"),
    )


def presigned_put(key: str, content_type: str = "application/zip") -> dict[str, Any]:
    client = s3_client()
    return {
        "url": client.generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=settings.presign_ttl_seconds,
        ),
        "method": "PUT",
        "headers": {"Content-Type": content_type},
        "expires_in": settings.presign_ttl_seconds,
    }


def presigned_get(key: str) -> dict[str, Any]:
    client = s3_client()
    return {
        "url": client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=settings.presign_ttl_seconds,
        ),
        "method": "GET",
        "expires_in": settings.presign_ttl_seconds,
    }


def download_s3(key: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3_client().download_file(settings.s3_bucket, key, str(target))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(status_code=400, detail=f"S3 object not found: {key}") from exc
        raise


def upload_s3_file(path: Path, key: str, content_type: str = "application/octet-stream") -> None:
    s3_client().upload_file(str(path), settings.s3_bucket, key, ExtraArgs={"ContentType": content_type})


def create_dataset_upload(owner_id: str, name: str, visibility: str) -> dict[str, Any]:
    if visibility not in {"private", "public"}:
        raise HTTPException(status_code=400, detail="visibility must be private or public")
    dataset_id = str(uuid.uuid4())
    key = f"datasets/{dataset_id}/bundle.zip"
    created_at = now_iso()
    with connect() as db:
        db.execute(
            "insert into datasets (id, owner_id, name, visibility, task_count, s3_key, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (dataset_id, owner_id, name, visibility, 0, key, created_at),
        )
    return {
        "dataset": {"id": dataset_id, "name": name, "visibility": visibility, "task_count": 0, "created_at": created_at},
        "upload": presigned_put(key),
    }


def finalize_dataset(dataset_id: str, owner_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("select * from datasets where id = ? and owner_id = ?", (dataset_id, owner_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bundle = root / "bundle.zip"
        extract = root / "extracted"
        download_s3(row["s3_key"], bundle)
        safe_extract_zip(bundle, extract)
        tasks = _discover_tasks(extract)
        with connect() as db:
            db.execute("delete from tasks where dataset_id = ?", (dataset_id,))
            db.execute("update datasets set task_count = ? where id = ?", (len(tasks), dataset_id))
            for task in tasks:
                db.execute(
                    "insert into tasks (id, dataset_id, title, version, metadata_json, task_path) values (?, ?, ?, ?, ?, ?)",
                    (task["id"], dataset_id, task["title"], int(task["version"]), json.dumps(task["metadata"]), task["relative_path"]),
                )
    return {"id": dataset_id, "name": row["name"], "visibility": row["visibility"], "task_count": len(tasks), "created_at": row["created_at"]}


def create_submission_upload(owner_id: str, name: str) -> dict[str, Any]:
    submission_id = str(uuid.uuid4())
    key = f"submissions/{submission_id}/submission.zip"
    created_at = now_iso()
    with connect() as db:
        db.execute(
            "insert into submissions (id, owner_id, name, status, score, s3_key, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (submission_id, owner_id, name, "uploading", None, key, created_at),
        )
    return {
        "submission": {"id": submission_id, "name": name, "status": "uploading", "score": None, "created_at": created_at},
        "upload": presigned_put(key),
    }


def finalize_submission(submission_id: str, owner_id: str, dataset_id: str | None = None) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("select * from submissions where id = ? and owner_id = ?", (submission_id, owner_id)).fetchone()
        dataset_rows = db.execute(
            "select id from datasets where (? is null or id = ?) and (owner_id = ? or visibility = 'public') and task_count > 0",
            (dataset_id, dataset_id, owner_id),
        ).fetchall()
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "submission.zip"
        extract = Path(tmp) / "extracted"
        download_s3(row["s3_key"], bundle)
        safe_extract_zip(bundle, extract)
        if not ((extract / "agent").exists() or (extract / "agent.py").exists()):
            raise HTTPException(status_code=400, detail="Submission must contain agent or agent.py")
    now = now_iso()
    with connect() as db:
        db.execute("update submissions set status = ? where id = ?", ("queued", submission_id))
        for dataset in dataset_rows:
            tasks = db.execute("select id from tasks where dataset_id = ?", (dataset["id"],)).fetchall()
            for task in tasks:
                db.execute(
                    "insert into jobs (id, submission_id, dataset_id, task_id, status, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), submission_id, dataset["id"], task["id"], "queued", now, now),
                )
    return {"id": submission_id, "name": row["name"], "status": "queued", "score": None, "created_at": row["created_at"]}


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            destination = (target_dir / member.filename).resolve()
            if not str(destination).startswith(str(target_dir.resolve())):
                raise HTTPException(status_code=400, detail="Unsafe ZIP path")
        archive.extractall(target_dir)


def make_zip(source_dir: Path, target_zip: Path) -> None:
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir))


def copy_task_data(dataset_key: str, task_id: str, target: Path) -> Path:
    bundle = target / "dataset.zip"
    extracted = target / "dataset"
    download_s3(dataset_key, bundle)
    safe_extract_zip(bundle, extracted)
    task_path = None
    for candidate in extracted.rglob("task.md"):
        text = candidate.read_text(encoding="utf-8")
        metadata = {}
        if text.startswith("---\n"):
            _, frontmatter, _body = text.split("---", 2)
            metadata = yaml.safe_load(frontmatter) or {}
        if str(metadata.get("id") or candidate.parent.name) == task_id:
            task_path = candidate
            break
    if task_path is None:
        raise RuntimeError(f"Task {task_id} not found in dataset bundle")
    task_root = task_path.parent
    shutil.copytree(task_root, target / "task", dirs_exist_ok=True)
    return target / "task"


def _discover_tasks(root: Path) -> list[dict[str, Any]]:
    tasks = []
    for path in root.rglob("task.md"):
        text = path.read_text(encoding="utf-8")
        metadata = {}
        if text.startswith("---\n"):
            _, frontmatter, _body = text.split("---", 2)
            metadata = yaml.safe_load(frontmatter) or {}
        task_id = str(metadata.get("id") or path.parent.name)
        tasks.append(
            {
                "id": task_id,
                "title": str(metadata.get("title") or task_id),
                "version": int(metadata.get("version") or 1),
                "metadata": metadata,
                "relative_path": str(path.relative_to(root)),
            }
        )
    if not tasks:
        raise HTTPException(status_code=400, detail="Dataset bundle must include at least one task.md")
    return tasks
