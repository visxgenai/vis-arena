from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import authenticate, create_token, create_user, current_user
from .db import connect, decode_json, init_db, row_to_dict
from .schemas import AuthResponse, LLMTokenRequest, LLMTokenResponse, LoginRequest, RegisterRequest
from .settings import settings
from .storage import create_dataset_upload, create_submission_upload, finalize_dataset, finalize_submission, presigned_get

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
            "select id, name, visibility, task_count, created_at from datasets where owner_id = ? or visibility = 'public' order by created_at desc",
            (user["id"],),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/v1/datasets/uploads")
def create_dataset_presigned_upload(payload: dict, user: dict = Depends(current_user)) -> dict:
    return create_dataset_upload(user["id"], payload["name"], payload.get("visibility", "private"))


@app.post("/v1/datasets/{dataset_id}/finalize")
def finalize_dataset_upload(dataset_id: str, user: dict = Depends(current_user)) -> dict:
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
            "select id, submission_id, dataset_id, task_id, status, result_json, artifact_s3_prefix, error, created_at, updated_at from jobs where submission_id = ? order by created_at desc",
            (submission_id,),
        ).fetchall()
    return {"items": [{**dict(row), "result": decode_json(row["result_json"], None)} for row in rows]}


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


@app.get("/v1/leaderboard")
def leaderboard() -> dict:
    with connect() as db:
        rows = db.execute(
            """
            select submissions.id, submissions.name, submissions.score, users.name as owner_name
            from submissions join users on users.id = submissions.owner_id
            where submissions.status = 'succeeded' and submissions.score is not null
            order by submissions.score desc
            limit 100
            """
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


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


def run() -> None:
    import uvicorn

    uvicorn.run("vis_arena_server.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=True)
