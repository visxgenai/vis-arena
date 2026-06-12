from __future__ import annotations

import os
import posixpath
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import jwt
from fastapi import Depends, HTTPException, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .auth import authenticate, create_token, create_user, current_user, update_user_name
from .db import connect, decode_json, init_db, row_to_dict
from .llm import create_llm_message
from .rounds import close_round, get_round_detail, list_rounds, open_round, round_leaderboard, start_peer_review
from .schemas import AuthResponse, LLMMessageRequest, LLMMessageResponse, LLMTokenRequest, LLMTokenResponse, LoginRequest, RegisterRequest, UpdateMeRequest, UserResponse
from .settings import settings
from .storage import create_dataset_upload, create_submission_upload, finalize_dataset, finalize_submission, presigned_get, read_s3_file

app = FastAPI(title="Vis Arena API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "VIS_ARENA_CORS_ORIGINS",
        "http://localhost:8200,http://arch:8200,https://vis-arena.jacobsun.xyz",
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/version")
def version() -> dict[str, str]:
    return {
        "server_version": "0.1.0",
        "latest_cli_version": os.environ.get("VIS_ARENA_LATEST_CLI_VERSION", "0.1.0"),
        "minimum_cli_version": os.environ.get("VIS_ARENA_MINIMUM_CLI_VERSION", "0.1.0"),
        "update_command": (
            'pip install --upgrade --force-reinstall '
            '"git+https://github.com/visxgenai/vis-arena.git#subdirectory=packages/arena-sdk"'
        ),
    }


def require_admin(user: dict) -> None:
    admin_emails = {
        email.strip().lower()
        for email in os.environ.get("VIS_ARENA_ADMIN_EMAILS", "").split(",")
        if email.strip()
    }
    if user["email"].lower() not in admin_emails:
        raise HTTPException(status_code=403, detail="Admin access required")


@app.post("/v1/auth/register", response_model=AuthResponse)
def register(payload: RegisterRequest) -> dict:
    try:
        user = create_user(payload.email, payload.password, payload.name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Email is already registered") from exc
    return {"access_token": create_token(user["id"]), "token_type": "bearer", "user": user}


@app.post("/v1/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> dict:
    user = authenticate(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"access_token": create_token(user["id"]), "token_type": "bearer", "user": user}


@app.get("/v1/me", response_model=UserResponse)
def me(user: dict = Depends(current_user)) -> dict:
    return user


@app.patch("/v1/me", response_model=UserResponse)
def update_me(payload: UpdateMeRequest, user: dict = Depends(current_user)) -> dict:
    return update_user_name(user["id"], payload.name)


@app.get("/v1/datasets")
def list_datasets(user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        rows = db.execute(
            """
            select id, name, visibility, task_count, created_at
            from datasets
            where (owner_id = ? or visibility = 'public') and task_count > 0
            order by created_at desc
            """,
            (user["id"],),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/v1/datasets/uploads")
def create_dataset_presigned_upload(payload: dict, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    return create_dataset_upload(user["id"], payload["name"], payload.get("visibility", "private"))


@app.post("/v1/datasets/{dataset_id}/finalize")
def finalize_dataset_upload(dataset_id: str, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    return finalize_dataset(dataset_id, user["id"])


@app.get("/v1/datasets/{dataset_id}/tasks")
def list_tasks(dataset_id: str, user: dict = Depends(current_user)) -> dict:
    _require_dataset_access(dataset_id, user["id"])
    with connect() as db:
        rows = db.execute("select id, dataset_id, title, version, metadata_json from tasks where dataset_id = ?", (dataset_id,)).fetchall()
    return {"items": [{**dict(row), "metadata": decode_json(row["metadata_json"], {})} for row in rows]}


@app.get("/v1/datasets/{dataset_id}/download")
def download_dataset(dataset_id: str, user: dict = Depends(current_user)) -> dict:
    dataset = _require_dataset_access(dataset_id, user["id"])
    return presigned_get(dataset["s3_key"])


@app.post("/v1/submissions/uploads")
def create_submission_presigned_upload(payload: dict, user: dict = Depends(current_user)) -> dict:
    return create_submission_upload(user["id"], payload["name"])


@app.post("/v1/submissions/{submission_id}/finalize")
def finalize_submission_upload(submission_id: str, payload: dict | None = None, user: dict = Depends(current_user)) -> dict:
    payload = payload or {}
    return finalize_submission(submission_id, user["id"], payload.get("dataset_id"))


@app.get("/v1/submissions")
def list_submissions(user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        rows = db.execute(
            "select id, name, status, score, created_at from submissions where owner_id = ? order by created_at desc",
            (user["id"],),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/v1/submissions/{submission_id}")
def get_submission(submission_id: str, user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        row = db.execute(
            "select id, name, status, score, created_at from submissions where id = ? and owner_id = ?",
            (submission_id, user["id"]),
        ).fetchone()
    submission = row_to_dict(row)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


@app.get("/v1/submissions/{submission_id}/jobs")
def list_submission_jobs(submission_id: str, user: dict = Depends(current_user)) -> dict:
    _require_submission_access(submission_id, user["id"])
    with connect() as db:
        rows = db.execute(
            """
            select id, submission_id, job_type, round_id, generator_submission_id,
                   review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
                   dataset_id, task_id, status, result_json,
                   artifact_s3_prefix, preview_s3_key, generation_s3_prefix,
                   evaluation_s3_prefix, agent_info_s3_key,
                   generation_trajectory_s3_key, evaluation_trajectory_s3_key,
                   generation_agent_trajectory_s3_key, evaluation_agent_trajectory_s3_key,
                   evaluation_report_s3_key, started_at,
                   completed_at, run_seconds, generation_run_seconds,
                   self_evaluation_run_seconds, error, created_at, updated_at
            from jobs
            where (coalesce(job_type, 'generation') = 'generation' and submission_id = ?)
               or (job_type in ('peer_review', 'peer_evaluation', 'central_evaluation') and generator_submission_id = ?)
            order by created_at desc
            """,
            (submission_id, submission_id),
        ).fetchall()
        usage_by_job = _llm_usage_by_job(db, [row["id"] for row in rows])
    items = []
    for row in rows:
        item = dict(row)
        item["result"] = decode_json(item.pop("result_json"), None)
        item["score"] = item["result"].get("score") if isinstance(item["result"], dict) else None
        usage = usage_by_job.get(item["id"], _empty_usage_breakdown())
        item["usage"] = usage["summary"]
        item["usage_by_purpose"] = usage["by_purpose"]
        item["generation_usage"] = usage["by_purpose"].get("generation", _empty_usage())
        item["self_evaluation_usage"] = usage["by_purpose"].get("evaluation", _empty_usage())
        items.append(item)
    return {"items": items}


@app.get("/v1/submissions/{submission_id}/llm-usage")
def get_submission_llm_usage(submission_id: str, user: dict = Depends(current_user)) -> dict:
    _require_submission_access(submission_id, user["id"])
    with connect() as db:
        summary = db.execute(
            """
            select
              count(*) as request_count,
              coalesce(sum(input_tokens), 0) as input_tokens,
              coalesce(sum(output_tokens), 0) as output_tokens,
              coalesce(sum(total_tokens), 0) as total_tokens,
              sum(estimated_cost_usd) as estimated_cost_usd
            from llm_usage
            where submission_id = ?
            """,
            (submission_id,),
        ).fetchone()
        by_job = db.execute(
            """
            select
              job_id,
              count(*) as request_count,
              coalesce(sum(input_tokens), 0) as input_tokens,
              coalesce(sum(output_tokens), 0) as output_tokens,
              coalesce(sum(total_tokens), 0) as total_tokens,
              sum(estimated_cost_usd) as estimated_cost_usd
            from llm_usage
            where submission_id = ?
            group by job_id
            order by max(created_at) desc
            """,
            (submission_id,),
        ).fetchall()
        by_purpose = db.execute(
            """
            select
              purpose,
              count(*) as request_count,
              coalesce(sum(input_tokens), 0) as input_tokens,
              coalesce(sum(output_tokens), 0) as output_tokens,
              coalesce(sum(total_tokens), 0) as total_tokens,
              sum(estimated_cost_usd) as estimated_cost_usd
            from llm_usage
            where submission_id = ?
            group by purpose
            order by purpose
            """,
            (submission_id,),
        ).fetchall()
        usage_by_job = _llm_usage_by_job(db, [row["job_id"] for row in by_job])
    jobs = []
    for row in by_job:
        item = dict(row)
        item["by_purpose"] = usage_by_job.get(item["job_id"], _empty_usage_breakdown())["by_purpose"]
        jobs.append(item)
    return {
        "summary": dict(summary),
        "by_purpose": {row["purpose"]: _usage_from_row(row) for row in by_purpose},
        "jobs": jobs,
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "request_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": None,
    }


def _empty_usage_breakdown() -> dict[str, Any]:
    return {"summary": _empty_usage(), "by_purpose": {}}


def _usage_from_row(row) -> dict[str, Any]:
    return {
        "request_count": int(row["request_count"] or 0),
        "input_tokens": int(row["input_tokens"] or 0),
        "output_tokens": int(row["output_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "estimated_cost_usd": row["estimated_cost_usd"],
    }


def _add_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    cost = None
    if left["estimated_cost_usd"] is not None or right["estimated_cost_usd"] is not None:
        cost = float(left["estimated_cost_usd"] or 0) + float(right["estimated_cost_usd"] or 0)
    return {
        "request_count": left["request_count"] + right["request_count"],
        "input_tokens": left["input_tokens"] + right["input_tokens"],
        "output_tokens": left["output_tokens"] + right["output_tokens"],
        "total_tokens": left["total_tokens"] + right["total_tokens"],
        "estimated_cost_usd": cost,
    }


def _llm_usage_by_job(db, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = db.execute(
        f"""
        select
          job_id,
          purpose,
          count(*) as request_count,
          coalesce(sum(input_tokens), 0) as input_tokens,
          coalesce(sum(output_tokens), 0) as output_tokens,
          coalesce(sum(total_tokens), 0) as total_tokens,
          sum(estimated_cost_usd) as estimated_cost_usd
        from llm_usage
        where job_id in ({placeholders})
        group by job_id, purpose
        """,
        job_ids,
    ).fetchall()
    usage_by_job = {job_id: _empty_usage_breakdown() for job_id in job_ids}
    for row in rows:
        usage = _usage_from_row(row)
        breakdown = usage_by_job.setdefault(row["job_id"], _empty_usage_breakdown())
        breakdown["by_purpose"][row["purpose"]] = usage
        breakdown["summary"] = _add_usage(breakdown["summary"], usage)
    return usage_by_job


@app.get("/v1/peer-reviews/rounds")
def api_list_rounds(limit: int = 20, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    return {"items": list_rounds(limit)}


@app.post("/v1/peer-reviews/rounds")
def api_open_round(payload: dict, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    return open_round(
        payload["name"],
        starts_at=payload.get("starts_at"),
        ends_at=payload.get("ends_at"),
        interval_seconds=payload.get("interval_seconds"),
    )


@app.get("/v1/peer-reviews/rounds/{round_id}")
def api_get_round(round_id: str, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    detail = get_round_detail(round_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Round not found")
    return detail


@app.post("/v1/peer-reviews/rounds/{round_id}/close")
def api_close_round(round_id: str, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    try:
        return close_round(round_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/peer-reviews/rounds/{round_id}/start-peer-review")
def api_start_peer_review(round_id: str, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    try:
        return start_peer_review(round_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/peer-reviews/rounds/{round_id}/leaderboard")
def api_round_leaderboard(round_id: str, limit: int = 100, user: dict = Depends(current_user)) -> dict:
    require_admin(user)
    return {"items": round_leaderboard(round_id, limit)}


@app.get("/v1/jobs/{job_id}/artifacts")
def get_job_artifacts(job_id: str, user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        row = db.execute(
            """
            select jobs.artifact_s3_prefix
            from jobs join submissions on submissions.id = jobs.submission_id
            where jobs.id = ? and submissions.owner_id = ?
            """,
            (job_id, user["id"]),
        ).fetchone()
    if row is None or not row["artifact_s3_prefix"]:
        raise HTTPException(status_code=404, detail="Artifacts not found")
    return presigned_get(f"{row['artifact_s3_prefix']}.zip")


@app.get("/v1/jobs/{job_id}/preview-url")
def get_job_preview_url(job_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    with connect() as db:
        row = db.execute(
            """
            select jobs.preview_s3_key
            from jobs join submissions on submissions.id = jobs.submission_id
            where jobs.id = ? and submissions.owner_id = ?
            """,
            (job_id, user["id"]),
        ).fetchone()
    if row is None or not row["preview_s3_key"]:
        raise HTTPException(status_code=404, detail="Preview not found")
    return {"url": str(request.url_for("redirect_job_preview", job_id=job_id)), "expires_in": settings.presign_ttl_seconds}


@app.get("/v1/jobs/{job_id}/preview")
def redirect_job_preview(job_id: str, request: Request) -> RedirectResponse:
    with connect() as db:
        row = db.execute("select preview_s3_key from jobs where id = ?", (job_id,)).fetchone()
    if row is None or not row["preview_s3_key"]:
        raise HTTPException(status_code=404, detail="Preview not found")
    token = _create_preview_token(job_id)
    url = str(request.url_for("serve_job_preview", job_id=job_id, asset_path="index.html"))
    return RedirectResponse(f"{url}?token={token}", status_code=302)


@app.get("/v1/jobs/{job_id}/preview/{asset_path:path}")
def serve_job_preview(job_id: str, asset_path: str, token: str) -> Response:
    _verify_preview_token(token, job_id)
    asset_path = _safe_preview_asset_path(asset_path or "index.html")
    with connect() as db:
        row = db.execute("select preview_s3_key from jobs where id = ?", (job_id,)).fetchone()
    if row is None or not row["preview_s3_key"]:
        raise HTTPException(status_code=404, detail="Preview not found")
    preview_prefix = row["preview_s3_key"].rsplit("/", 1)[0]
    body, content_type = read_s3_file(f"{preview_prefix}/{asset_path}")
    if content_type.startswith("text/html"):
        text = body.decode("utf-8", errors="replace")
        body = _rewrite_preview_asset_urls(text, job_id, token).encode("utf-8")
    elif content_type.startswith("text/css"):
        text = body.decode("utf-8", errors="replace")
        body = _rewrite_preview_css_urls(text, job_id, token).encode("utf-8")
    return Response(content=body, media_type=content_type)


@app.get("/v1/leaderboard")
def leaderboard(request: Request) -> dict:
    with connect() as db:
        rows = db.execute(
            """
            select submissions.id, submissions.name, submissions.score, users.name as owner_name,
                   (select j.id from jobs j
                    where j.submission_id = submissions.id
                      and coalesce(j.job_type, 'generation') = 'generation'
                      and j.preview_s3_key is not null
                    order by j.completed_at desc
                    limit 1) as preview_job_id
            from submissions join users on users.id = submissions.owner_id
            where submissions.status = 'succeeded' and submissions.score is not null
            order by submissions.score desc
            limit 100
            """
        ).fetchall()
        submission_ids = [row["id"] for row in rows]
        jobs_by_submission = _leaderboard_jobs_by_submission(db, request, submission_ids)
        participants = _leaderboard_participants(db, request)
        rounds = _leaderboard_rounds(db)
    items = []
    for row in rows:
        entry = dict(row)
        preview_job_id = entry.pop("preview_job_id", None)
        entry["preview_url"] = (
            str(request.url_for("redirect_job_preview", job_id=preview_job_id))
            if preview_job_id
            else None
        )
        entry["jobs"] = jobs_by_submission.get(entry["id"], [])
        items.append(entry)
    return {"items": items, "participants": participants, "rounds": rounds}


def _leaderboard_jobs_by_submission(db, request: Request, submission_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    jobs_by_submission: dict[str, list[dict[str, Any]]] = {submission_id: [] for submission_id in submission_ids}
    if not submission_ids:
        return jobs_by_submission
    placeholders = ",".join("?" for _ in submission_ids)
    job_rows = db.execute(
        f"""
        select id, submission_id, job_type, round_id, generator_submission_id,
               dataset_id, task_id, status, result_json, preview_s3_key,
               generation_s3_prefix, evaluation_s3_prefix, agent_info_s3_key,
               generation_trajectory_s3_key, evaluation_trajectory_s3_key,
               generation_agent_trajectory_s3_key, evaluation_agent_trajectory_s3_key,
               evaluation_report_s3_key, started_at, completed_at, run_seconds,
               generation_run_seconds, self_evaluation_run_seconds, error
        from jobs
        where submission_id in ({placeholders})
          and coalesce(job_type, 'generation') = 'generation'
        order by task_id, completed_at desc
        """,
        submission_ids,
    ).fetchall()
    job_ids = [row["id"] for row in job_rows]
    usage_by_job = _llm_usage_by_job(db, job_ids)
    evaluations_by_job = _leaderboard_evaluations_by_job(db, job_ids)
    for job_row in job_rows:
        job = dict(job_row)
        preview_job_id = job["id"] if job.get("preview_s3_key") else None
        job["result"] = decode_json(job.pop("result_json"), None)
        job["preview_url"] = (
            str(request.url_for("redirect_job_preview", job_id=preview_job_id))
            if preview_job_id
            else None
        )
        usage = usage_by_job.get(job["id"], _empty_usage_breakdown())
        job["usage"] = usage["summary"]
        job["usage_by_purpose"] = usage["by_purpose"]
        job["generation_usage"] = usage["by_purpose"].get("generation", _empty_usage())
        job["self_evaluation_usage"] = usage["by_purpose"].get("evaluation", _empty_usage())
        job["evaluations"] = evaluations_by_job.get(job["id"], [])
        jobs_by_submission.setdefault(job["submission_id"], []).append(job)
    return jobs_by_submission


def _leaderboard_evaluations_by_job(db, job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    evaluations_by_job: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in job_ids}
    if not job_ids:
        return evaluations_by_job
    placeholders = ",".join("?" for _ in job_ids)
    rows = db.execute(
        f"""
        select artifact_job_id, evaluator_type, evaluator_name, job_id, status, score, max_score,
               evaluation_report_s3_key, evaluation_trajectory_s3_key,
               run_seconds, completed_at
        from evaluations
        where artifact_job_id in ({placeholders})
        order by evaluator_type, completed_at
        """,
        job_ids,
    ).fetchall()
    usage_by_job = _llm_usage_by_job(db, [row["job_id"] for row in rows if row["job_id"]])
    for row in rows:
        item = dict(row)
        usage = usage_by_job.get(item.get("job_id"), _empty_usage_breakdown())
        item["evaluation_usage"] = usage["by_purpose"].get("evaluation", _empty_usage())
        evaluations_by_job.setdefault(item.pop("artifact_job_id"), []).append(item)
    return evaluations_by_job


def _leaderboard_participants(db, request: Request) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        select submissions.id, submissions.name, submissions.status, submissions.score,
               submissions.created_at, submissions.finalized_at,
               users.id as user_id, users.name as owner_name,
               (select j.id from jobs j
                where j.submission_id = submissions.id
                  and coalesce(j.job_type, 'generation') = 'generation'
                  and j.preview_s3_key is not null
                order by j.completed_at desc
                limit 1) as preview_job_id
        from submissions join users on users.id = submissions.owner_id
        where submissions.finalized_at is not null
        order by coalesce(users.name, users.id), submissions.finalized_at desc, submissions.created_at desc
        """
    ).fetchall()
    submission_ids = [row["id"] for row in rows]
    jobs_by_submission = _leaderboard_jobs_by_submission(db, request, submission_ids)
    participants: dict[str, dict[str, Any]] = {}
    for row in rows:
        submission = dict(row)
        user_id = submission.pop("user_id")
        owner_name = submission.pop("owner_name") or "Anonymous participant"
        preview_job_id = submission.pop("preview_job_id", None)
        submission["preview_url"] = (
            str(request.url_for("redirect_job_preview", job_id=preview_job_id))
            if preview_job_id
            else None
        )
        submission["jobs"] = jobs_by_submission.get(submission["id"], [])
        participant = participants.setdefault(
            user_id,
            {
                "user_id": user_id,
                "owner_name": owner_name,
                "submissions": [],
                "best_submission": None,
                "latest_submission": None,
                "best_score": None,
                "latest_score": None,
                "rank": None,
            },
        )
        participant["submissions"].append(submission)
        if participant["latest_submission"] is None:
            participant["latest_submission"] = submission
            participant["latest_score"] = submission.get("score")
        if submission.get("score") is not None and (
            participant["best_score"] is None or submission["score"] > participant["best_score"]
        ):
            participant["best_score"] = submission["score"]
            participant["best_submission"] = submission
    ordered = sorted(
        participants.values(),
        key=lambda item: (
            item["best_score"] is None,
            -(item["best_score"] or 0),
            str(item["owner_name"]).lower(),
        ),
    )
    for index, participant in enumerate(ordered, start=1):
        participant["rank"] = index if participant["best_score"] is not None else None
    return ordered


def _leaderboard_rounds(db) -> list[dict[str, Any]]:
    rounds = db.execute(
        """
        select *
        from review_rounds
        order by coalesce(starts_at, created_at)
        limit 50
        """
    ).fetchall()
    items = []
    previous_by_user: dict[str, dict[str, Any]] = {}
    for index, round_row in enumerate(rounds, start=1):
        round_item = dict(round_row)
        scores = round_leaderboard(round_item["id"], limit=100000)
        score_by_user = {score["user_id"]: score for score in scores}
        participants = db.execute(
            """
            select rp.user_id, rp.submission_id, rp.selection_reason,
                   users.name as owner_name,
                   submissions.name as submission_name
            from round_participants rp
            join users on users.id = rp.user_id
            join submissions on submissions.id = rp.submission_id
            where rp.round_id = ?
            order by coalesce(users.name, users.id)
            """,
            (round_item["id"],),
        ).fetchall()
        ranked_scores = sorted(
            [score for score in scores if score.get("round_score") is not None],
            key=lambda item: item["round_score"],
            reverse=True,
        )
        rank_by_user = {score["user_id"]: rank for rank, score in enumerate(ranked_scores, start=1)}
        timeline = []
        for participant in participants:
            row = dict(participant)
            score = score_by_user.get(row["user_id"], {})
            previous = previous_by_user.get(row["user_id"])
            rank = rank_by_user.get(row["user_id"])
            timeline_item = {
                **row,
                "owner_name": row.get("owner_name") or "Anonymous participant",
                "round_score": score.get("round_score"),
                "rank": rank,
                "score_delta": (
                    score.get("round_score") - previous["round_score"]
                    if previous and score.get("round_score") is not None and previous.get("round_score") is not None
                    else None
                ),
                "rank_delta": (
                    previous["rank"] - rank
                    if previous and rank is not None and previous.get("rank") is not None
                    else None
                ),
                "is_new_submission": row["selection_reason"] == "interval_latest",
                "carried_over": row["selection_reason"] == "carried_forward",
            }
            timeline.append(timeline_item)
            if score.get("round_score") is not None:
                previous_by_user[row["user_id"]] = timeline_item
        round_item["index"] = index
        round_item["participants"] = timeline
        items.append(round_item)
    return items


@app.post("/v1/llm/token", response_model=LLMTokenResponse)
def llm_token(payload: LLMTokenRequest, user: dict = Depends(current_user)) -> dict:
    if not settings.cloud_llm_enabled:
        raise HTTPException(
            status_code=403,
            detail="Cloud LLM brokerage is disabled in this deployment. Use your own provider keys for local testing.",
        )
    if payload.provider != "openai" or not settings.brokered_openai_api_key:
        raise HTTPException(status_code=400, detail="Only brokered OpenAI tokens are configured in this scaffold")
    # Production should mint a scoped proxy token and audit user/provider/model/purpose.
    return {
        "provider": payload.provider,
        "model": payload.model,
        "access_token": settings.brokered_openai_api_key,
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
        "base_url": None,
    }


@app.post("/v1/llm/messages", response_model=LLMMessageResponse)
def llm_messages(payload: LLMMessageRequest, user: dict = Depends(current_user)) -> dict:
    return create_llm_message(payload, user["id"])


def _require_dataset_access(dataset_id: str, user_id: str) -> dict:
    with connect() as db:
        row = db.execute(
            "select * from datasets where id = ? and (owner_id = ? or visibility = 'public')",
            (dataset_id, user_id),
        ).fetchone()
    dataset = row_to_dict(row)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


def _require_submission_access(submission_id: str, user_id: str) -> dict:
    with connect() as db:
        row = db.execute(
            "select * from submissions where id = ? and owner_id = ?",
            (submission_id, user_id),
        ).fetchone()
    submission = row_to_dict(row)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


def _create_preview_token(job_id: str) -> str:
    payload = {
        "job": job_id,
        "scope": "artifact-preview",
        "exp": datetime.now(UTC) + timedelta(seconds=settings.presign_ttl_seconds),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _verify_preview_token(token: str, job_id: str) -> None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid preview token") from exc
    if payload.get("scope") != "artifact-preview" or payload.get("job") != job_id:
        raise HTTPException(status_code=401, detail="Invalid preview token")


def _safe_preview_asset_path(asset_path: str) -> str:
    normalized = posixpath.normpath(asset_path).lstrip("/")
    if normalized == "." or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise HTTPException(status_code=400, detail="Unsafe preview path")
    return normalized


def _rewrite_preview_asset_urls(html: str, job_id: str, token: str) -> str:
    return re.sub(
        r"""(?P<attr>\b(?:src|href)=)(?P<quote>["'])(?P<url>[^"']+)(?P=quote)""",
        lambda match: _rewrite_attr_match(match, job_id, token),
        html,
    )


def _rewrite_preview_css_urls(css: str, job_id: str, token: str) -> str:
    return re.sub(
        r"""url\((?P<quote>["']?)(?P<url>[^)"']+)(?P=quote)\)""",
        lambda match: f"url({match.group('quote')}{_preview_asset_url(match.group('url'), job_id, token)}{match.group('quote')})",
        css,
    )


def _rewrite_attr_match(match: re.Match[str], job_id: str, token: str) -> str:
    url = _preview_asset_url(match.group("url"), job_id, token)
    return f"{match.group('attr')}{match.group('quote')}{url}{match.group('quote')}"


def _preview_asset_url(url: str, job_id: str, token: str) -> str:
    if _is_external_preview_url(url):
        return url
    path, fragment = url.split("#", 1) if "#" in url else (url, "")
    path = path.split("?", 1)[0]
    # Use a path relative to the current document so the same HTML works when
    # served directly from the backend (`/v1/jobs/<id>/preview/index.html`)
    # AND when served through a Next.js-style proxy at
    # `/api/arena/v1/jobs/<id>/preview/index.html`. Absolute paths break the
    # latter case because they bypass the proxy mount point.
    safe = _safe_preview_asset_path(path)
    rewritten = f"{quote(safe)}?token={quote(token)}"
    return f"{rewritten}#{fragment}" if fragment else rewritten


def _is_external_preview_url(url: str) -> bool:
    lowered = url.lower()
    return (
        not url
        or lowered.startswith(("http://", "https://", "data:", "blob:", "mailto:", "tel:", "javascript:"))
        or lowered.startswith("//")
        or lowered.startswith("#")
    )


def run() -> None:
    import uvicorn

    uvicorn.run("vis_arena_server.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=True)
