from __future__ import annotations

import os
import posixpath
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import jwt
from fastapi import Depends, HTTPException, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .auth import authenticate, create_token, create_user, current_user
from .db import connect, decode_json, init_db, row_to_dict
from .llm import create_llm_message
from .schemas import AuthResponse, LLMMessageRequest, LLMMessageResponse, LLMTokenRequest, LLMTokenResponse, LoginRequest, RegisterRequest
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


@app.get("/v1/me")
def me(user: dict = Depends(current_user)) -> dict:
    return user


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
            select id, submission_id, dataset_id, task_id, status, result_json,
                   artifact_s3_prefix, preview_s3_key, generation_s3_prefix,
                   evaluation_s3_prefix, agent_info_s3_key,
                   generation_trajectory_s3_key, evaluation_trajectory_s3_key,
                   evaluation_report_s3_key, started_at,
                   completed_at, run_seconds, error, created_at, updated_at
            from jobs
            where submission_id = ?
            order by created_at desc
            """,
            (submission_id,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["result"] = decode_json(item.pop("result_json"), None)
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
    return {"summary": dict(summary), "jobs": [dict(row) for row in by_job]}


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
                    where j.submission_id = submissions.id and j.preview_s3_key is not null
                    order by j.completed_at desc
                    limit 1) as preview_job_id
            from submissions join users on users.id = submissions.owner_id
            where submissions.status = 'succeeded' and submissions.score is not null
            order by submissions.score desc
            limit 100
            """
        ).fetchall()
    items = []
    for row in rows:
        entry = dict(row)
        preview_job_id = entry.pop("preview_job_id", None)
        entry["preview_url"] = (
            str(request.url_for("redirect_job_preview", job_id=preview_job_id))
            if preview_job_id
            else None
        )
        items.append(entry)
    return {"items": items}


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
    rewritten = f"/v1/jobs/{job_id}/preview/{quote(_safe_preview_asset_path(path))}?token={quote(token)}"
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
