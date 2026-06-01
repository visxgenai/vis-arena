from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .auth import create_token
from .db import connect, now_iso, row_to_dict
from .settings import settings
from .storage import copy_task_data, download_s3, make_zip, safe_extract_zip, upload_s3_directory, upload_s3_file


def run() -> None:
    while True:
        job = claim_job()
        if job is None:
            time.sleep(3)
            continue
        try:
            result = run_job(job)
            complete_job(job["id"], result)
        except Exception as exc:
            fail_job(job["id"], exc)


def claim_job() -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            """
            select jobs.*, submissions.owner_id, submissions.s3_key as submission_s3_key, datasets.s3_key as dataset_s3_key
            from jobs
            join submissions on submissions.id = jobs.submission_id
            join datasets on datasets.id = jobs.dataset_id
            where jobs.status = 'queued'
            order by jobs.created_at
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        now = now_iso()
        db.execute("update jobs set status = ?, updated_at = ? where id = ?", ("running", now, row["id"]))
        db.execute("update submissions set status = ? where id = ?", ("running", row["submission_id"]))
        return dict(row)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        submission_zip = root / "submission.zip"
        submission_dir = root / "submission"
        work_dir = root / "work"
        reports_dir = root / "reports"
        artifacts_zip = root / "artifacts.zip"

        download_s3(job["submission_s3_key"], submission_zip)
        safe_extract_zip(submission_zip, submission_dir)
        copy_sdk(root / "sdk")
        copy_task_data(job["dataset_s3_key"], job["task_id"], work_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)

        script = render_container_script()
        (root / "run.sh").write_text(script, encoding="utf-8")
        os.chmod(root / "run.sh", 0o755)
        run_docker(root, job)

        evaluation = json.loads((reports_dir / "evaluation.json").read_text(encoding="utf-8"))
        make_zip(work_dir, artifacts_zip)
        artifact_prefix = f"jobs/{job['id']}/artifacts"
        preview_prefix = f"jobs/{job['id']}/preview"
        preview_s3_key = None
        upload_s3_file(artifacts_zip, f"{artifact_prefix}.zip", "application/zip")
        preview_dist = work_dir / "output" / "dist"
        if (preview_dist / "index.html").exists():
            upload_s3_directory(preview_dist, preview_prefix)
            preview_s3_key = f"{preview_prefix}/index.html"
        return {
            "evaluation": evaluation,
            "score": evaluation.get("score"),
            "artifact_s3_prefix": artifact_prefix,
            "preview_s3_key": preview_s3_key,
        }


def copy_sdk(target: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    shutil.copytree(
        repo_root / "packages" / "arena-sdk",
        target,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.egg-info"),
    )


def render_container_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
mkdir -p /arena/home /arena/.uv-cache /arena/.venv
export PATH="/arena/home/.local/bin:$PATH"
cd /arena/submission
if [ -f pyproject.toml ]; then
  python -m pip install --upgrade pip uv >/tmp/pip.log 2>&1 || cat /tmp/pip.log
  uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py info --output /arena/work/agent-info.json
  uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py generate --task /arena/work/task/task.md --data-dir /arena/work/task/data --output-dir /arena/work/output
  uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py evaluate --task /arena/work/task/task.md --data-dir /arena/work/task/data --source-dir /arena/work/output/source --dist-dir /arena/work/output/dist --output /arena/reports/evaluation.json
else
  ./agent info --output /arena/work/agent-info.json
  ./agent generate --task /arena/work/task/task.md --data-dir /arena/work/task/data --output-dir /arena/work/output
  ./agent evaluate --task /arena/work/task/task.md --data-dir /arena/work/task/data --source-dir /arena/work/output/source --dist-dir /arena/work/output/dist --output /arena/reports/evaluation.json
fi
"""


def run_docker(root: Path, job: dict[str, Any]) -> None:
    arena_token = create_token(job["owner_id"])
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        settings.evaluator_network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-e",
        "HOME=/arena/home",
        "-e",
        "UV_CACHE_DIR=/arena/.uv-cache",
        "-e",
        "UV_PROJECT_ENVIRONMENT=/arena/.venv",
        "-e",
        f"VIS_ARENA_SERVER_URL={_container_server_url()}",
        "-e",
        f"VIS_ARENA_API_TOKEN={arena_token}",
        "-e",
        f"VIS_ARENA_JOB_ID={job['id']}",
        "-e",
        f"VIS_ARENA_LLM_PROVIDER={settings.llm_provider}",
        "-e",
        f"VIS_ARENA_LLM_MODEL={settings.bedrock_default_model_id if settings.llm_provider == 'bedrock' else os.environ.get('VIS_ARENA_OPENAI_MODEL', 'gpt-4.1-mini')}",
        "-e",
        f"VIS_ARENA_LLM_MODELS={','.join(settings.bedrock_model_ids) if settings.llm_provider == 'bedrock' else os.environ.get('VIS_ARENA_OPENAI_MODEL', 'gpt-4.1-mini')}",
        "-v",
        f"{root}:/arena",
        "-w",
        "/arena",
        settings.evaluator_image,
        "bash",
        "/arena/run.sh",
    ]
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=settings.evaluator_timeout_seconds)
    (root / "reports" / "docker.log").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"Docker evaluation failed with exit {completed.returncode}:\n{completed.stdout[-4000:]}")


def _container_server_url() -> str:
    if settings.public_base_url in {"http://localhost:8000", "http://127.0.0.1:8000"}:
        return "http://host.docker.internal:8000"
    return settings.public_base_url


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    now = now_iso()
    with connect() as db:
        job = db.execute("select submission_id from jobs where id = ?", (job_id,)).fetchone()
        db.execute(
            "update jobs set status = ?, result_json = ?, artifact_s3_prefix = ?, preview_s3_key = ?, updated_at = ? where id = ?",
            ("succeeded", json.dumps(result["evaluation"]), result["artifact_s3_prefix"], result["preview_s3_key"], now, job_id),
        )
        scores = [
            row["score"]
            for row in db.execute("select json_extract(result_json, '$.score') as score from jobs where submission_id = ? and status = 'succeeded'", (job["submission_id"],))
            if row["score"] is not None
        ]
        remaining = db.execute("select count(*) as count from jobs where submission_id = ? and status in ('queued', 'running')", (job["submission_id"],)).fetchone()["count"]
        if remaining == 0:
            avg = sum(float(score) for score in scores) / len(scores) if scores else None
            status = "succeeded" if scores else "failed"
            db.execute("update submissions set status = ?, score = ? where id = ?", (status, avg, job["submission_id"]))


def fail_job(job_id: str, exc: Exception) -> None:
    now = now_iso()
    with connect() as db:
        job = row_to_dict(db.execute("select submission_id from jobs where id = ?", (job_id,)).fetchone())
        db.execute("update jobs set status = ?, error = ?, updated_at = ? where id = ?", ("failed", str(exc), now, job_id))
        if job:
            remaining = db.execute("select count(*) as count from jobs where submission_id = ? and status in ('queued', 'running')", (job["submission_id"],)).fetchone()["count"]
            if remaining == 0:
                db.execute("update submissions set status = ? where id = ?", ("failed", job["submission_id"]))
