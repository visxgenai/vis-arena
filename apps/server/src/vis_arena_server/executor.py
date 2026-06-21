from __future__ import annotations

import re
from typing import Any

import boto3

from .auth import create_job_token
from .db import connect, now_iso, row_to_dict
from .settings import settings

LOCAL_DOCKER = "local_docker"
AWS_BATCH_FARGATE = "aws_batch_fargate"
EXECUTOR_MODES = {LOCAL_DOCKER, AWS_BATCH_FARGATE}


def configured_executor() -> str:
    mode = settings.executor_mode
    if mode not in EXECUTOR_MODES:
        raise RuntimeError(f"Unsupported VIS_ARENA_EXECUTOR_MODE: {mode}")
    return mode


def dispatch_queued_jobs(limit: int = 50) -> list[dict[str, Any]]:
    if configured_executor() != AWS_BATCH_FARGATE:
        return []
    dispatched: list[dict[str, Any]] = []
    for job in _undispatched_batch_jobs(limit):
        dispatched.append(dispatch_job(job["id"]))
    return dispatched


def dispatch_job(job_id: str) -> dict[str, Any]:
    if configured_executor() != AWS_BATCH_FARGATE:
        return {"job_id": job_id, "executor": LOCAL_DOCKER, "dispatched": False}
    _require_batch_settings()
    now = now_iso()
    with connect() as db:
        row = db.execute("select * from jobs where id = ?", (job_id,)).fetchone()
        job = row_to_dict(row)
        if not job:
            raise RuntimeError(f"Job not found: {job_id}")
        if job["status"] != "queued":
            return {"job_id": job_id, "executor": job.get("executor"), "dispatched": False}
        if job.get("external_job_id"):
            return {"job_id": job_id, "executor": AWS_BATCH_FARGATE, "external_job_id": job["external_job_id"], "dispatched": False}
        claimed = db.execute(
            """
            update jobs
            set executor = ?, external_job_id = ?, dispatched_at = ?, executor_error = null, updated_at = ?
            where id = ? and status = 'queued' and external_job_id is null
            """,
            (AWS_BATCH_FARGATE, f"submitting:{job_id}", now, now, job_id),
        ).rowcount
        if claimed != 1:
            return {"job_id": job_id, "executor": AWS_BATCH_FARGATE, "dispatched": False}

    try:
        response = _batch_client().submit_job(**_submit_job_payload(job_id))
        external_job_id = response["jobId"]
    except Exception as exc:
        with connect() as db:
            db.execute(
                """
                update jobs
                set external_job_id = null, executor_error = ?, updated_at = ?
                where id = ? and external_job_id = ?
                """,
                (f"AWS Batch submit failed: {exc}", now_iso(), job_id, f"submitting:{job_id}"),
            )
        raise

    with connect() as db:
        db.execute(
            """
            update jobs
            set external_job_id = ?, executor_error = null, updated_at = ?
            where id = ?
            """,
            (external_job_id, now_iso(), job_id),
        )
    return {"job_id": job_id, "executor": AWS_BATCH_FARGATE, "external_job_id": external_job_id, "dispatched": True}


def reconcile_batch_jobs(limit: int = 100) -> list[dict[str, Any]]:
    if configured_executor() != AWS_BATCH_FARGATE:
        return []
    with connect() as db:
        rows = db.execute(
            """
            select id, external_job_id
            from jobs
            where executor = ?
              and external_job_id is not null
              and external_job_id not like 'submitting:%'
              and status in ('queued', 'running')
            order by coalesce(dispatched_at, created_at)
            limit ?
            """,
            (AWS_BATCH_FARGATE, limit),
        ).fetchall()
    external_ids = [row["external_job_id"] for row in rows]
    if not external_ids:
        return []
    response = _batch_client().describe_jobs(jobs=external_ids)
    by_external_id = {job["jobId"]: job for job in response.get("jobs", [])}
    updates: list[dict[str, Any]] = []
    for row in rows:
        batch_job = by_external_id.get(row["external_job_id"])
        if batch_job is None:
            continue
        status = batch_job.get("status")
        if status == "FAILED":
            reason = batch_job.get("statusReason") or "AWS Batch job failed before runner callback"
            from .evaluator import fail_job

            fail_job(row["id"], RuntimeError(reason))
            updates.append({"job_id": row["id"], "external_job_id": row["external_job_id"], "status": status, "error": reason})
        elif status == "SUCCEEDED":
            with connect() as db:
                db.execute(
                    """
                    update jobs
                    set executor_error = ?, updated_at = ?
                    where id = ? and status in ('queued', 'running')
                    """,
                    ("AWS Batch job succeeded without completion callback", now_iso(), row["id"]),
                )
            updates.append({"job_id": row["id"], "external_job_id": row["external_job_id"], "status": status})
    return updates


def _undispatched_batch_jobs(limit: int) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            select *
            from jobs
            where status = 'queued'
              and coalesce(executor, ?) = ?
              and external_job_id is null
            order by case coalesce(job_type, 'generation') when 'generation' then 0 else 1 end,
                     created_at
            limit ?
            """,
            (AWS_BATCH_FARGATE, AWS_BATCH_FARGATE, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _submit_job_payload(job_id: str) -> dict[str, Any]:
    env = [
        {"name": "VIS_ARENA_JOB_ID", "value": job_id},
        {"name": "VIS_ARENA_RUNNER_TOKEN", "value": create_job_token(job_id)},
        {"name": "VIS_ARENA_SERVER_URL", "value": settings.public_base_url},
        {"name": "VIS_ARENA_RECORD_TRAJECTORY", "value": "true" if settings.record_trajectory else "false"},
    ]
    overrides: dict[str, Any] = {
        "environment": env,
        "resourceRequirements": [
            {"type": "VCPU", "value": settings.aws_batch_job_vcpus},
            {"type": "MEMORY", "value": settings.aws_batch_job_memory},
        ],
    }
    if settings.aws_batch_runner_image:
        overrides["image"] = settings.aws_batch_runner_image
    return {
        "jobName": _batch_job_name(job_id),
        "jobQueue": settings.aws_batch_job_queue,
        "jobDefinition": settings.aws_batch_job_definition,
        "containerOverrides": overrides,
        "timeout": {"attemptDurationSeconds": settings.aws_batch_job_timeout_seconds},
    }


def _batch_job_name(job_id: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id)[:96]
    return f"vis-arena-{suffix}"


def _batch_client():
    return boto3.client("batch", region_name=settings.aws_batch_region)


def _require_batch_settings() -> None:
    missing = []
    if not settings.aws_batch_job_queue:
        missing.append("VIS_ARENA_AWS_BATCH_JOB_QUEUE")
    if not settings.aws_batch_job_definition:
        missing.append("VIS_ARENA_AWS_BATCH_JOB_DEFINITION")
    if missing:
        raise RuntimeError(f"AWS Batch executor is missing required settings: {', '.join(missing)}")
