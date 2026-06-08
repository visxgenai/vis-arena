from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .auth import authenticate, create_token, create_user, current_user
from .db import connect, decode_json, init_db, row_to_dict
from .local_storage import local_file_path, local_save_bytes, local_storage_enabled
from .schemas import AuthResponse, LLMTokenRequest, LLMTokenResponse, LoginRequest, RegisterRequest
from .settings import settings
from .storage import create_dataset_upload, create_submission_upload, finalize_dataset, finalize_submission, presigned_get

app = FastAPI(title="Vis Arena API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "VIS_ARENA_CORS_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://arch:5173,http://arch:5174,http://arch:5175",
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


@app.put("/_local/upload/{key:path}")
async def local_upload(key: str, request: Request) -> dict[str, str]:
    """Receive a file upload destined for local storage."""
    if not local_storage_enabled():
        raise HTTPException(status_code=404, detail="Local storage is disabled")
    data = await request.body()
    local_save_bytes(data, key)
    return {"status": "ok"}


@app.get("/_local/files/{key:path}")
def local_download(key: str) -> FileResponse:
    """Serve a file from local storage."""
    if not local_storage_enabled():
        raise HTTPException(status_code=404, detail="Local storage is disabled")
    path = local_file_path(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="application/octet-stream")


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


def _criteria_for_submission(db, submission_id: str) -> list[dict]:
    job_rows = db.execute(
        "select result_json from jobs where submission_id = ? and status = 'succeeded' and result_json is not null",
        (submission_id,),
    ).fetchall()
    criteria_agg: dict[str, dict] = {}
    for jr in job_rows:
        result = decode_json(jr["result_json"], {})
        for c in result.get("criteria", []):
            cid = c.get("id", "")
            if not cid:
                continue
            if cid not in criteria_agg:
                criteria_agg[cid] = {"scores": [], "max_scores": []}
            criteria_agg[cid]["scores"].append(float(c.get("score", 0)))
            criteria_agg[cid]["max_scores"].append(float(c.get("max_score", 1)))
    return [
        {
            "id": k,
            "score": round(sum(v["scores"]) / len(v["scores"]), 2),
            "max_score": round(sum(v["max_scores"]) / len(v["max_scores"]), 2),
        }
        for k, v in criteria_agg.items()
    ]


@app.get("/v1/leaderboard")
def leaderboard() -> dict:
    with connect() as db:
        rows = db.execute(
            """
            select submissions.id, submissions.name, submissions.score,
                   submissions.created_at, users.name as owner_name
            from submissions join users on users.id = submissions.owner_id
            where submissions.status = 'succeeded' and submissions.score is not null
            order by submissions.score desc
            """
        ).fetchall()

        agents: dict[str, dict] = {}
        for row in rows:
            entry = dict(row)
            entry["criteria"] = _criteria_for_submission(db, row["id"])
            has_artifacts = db.execute(
                "select 1 from jobs where submission_id = ? and artifact_s3_prefix is not null limit 1",
                (row["id"],),
            ).fetchone()
            entry["has_preview"] = has_artifacts is not None

            agent_name = row["name"]
            if agent_name not in agents:
                agents[agent_name] = {**entry, "submissions": []}
            agents[agent_name]["submissions"].append({
                "id": entry["id"],
                "score": entry["score"],
                "created_at": entry["created_at"],
                "criteria": entry["criteria"],
                "has_preview": entry["has_preview"],
            })

        for agent in agents.values():
            agent["submissions"].sort(key=lambda s: s["created_at"] or "", reverse=True)

    items = sorted(agents.values(), key=lambda a: a.get("score") or 0, reverse=True)
    return {"items": items}


@app.get("/v1/leaderboard/history")
def leaderboard_history() -> dict:
    with connect() as db:
        rows = db.execute(
            """
            select submissions.name, submissions.score, submissions.created_at
            from submissions
            where submissions.status = 'succeeded' and submissions.score is not null
            order by submissions.created_at
            """
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/v1/leaderboard/{submission_id}/preview")
def leaderboard_preview(submission_id: str) -> dict:
    with connect() as db:
        row = db.execute(
            """
            select submissions.id from submissions
            where submissions.id = ? and submissions.status = 'succeeded' and submissions.score is not null
            """,
            (submission_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Submission not found on leaderboard")
        job = db.execute(
            "select artifact_s3_prefix from jobs where submission_id = ? and status = 'succeeded' and artifact_s3_prefix is not null limit 1",
            (submission_id,),
        ).fetchone()
    if job is None or not job["artifact_s3_prefix"]:
        raise HTTPException(status_code=404, detail="No artifacts available")
    return presigned_get(f"{job['artifact_s3_prefix']}.zip")


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
