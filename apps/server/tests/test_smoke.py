"""End-to-end smoke for the routes the evaluation-server-frontend BFF hits.

Mirrors what the Next.js shell does on first sign-up:
    POST /v1/auth/register
    POST /v1/auth/login
    GET  /v1/me
    GET  /v1/submissions   (empty list for a fresh user)

Run from repo root:
    pytest apps/server/tests
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient


def _unique_email() -> str:
    # Unique per-call so multiple test runs against the same temp DB don't collide.
    return f"smoke-{uuid.uuid4().hex[:12]}@example.com"


def test_register_login_me_submissions(client: TestClient) -> None:
    email = _unique_email()
    password = "correct-horse-battery-staple"
    name = "Smoke Test"

    # --- register ---
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "name": name},
    )
    assert res.status_code == 200, res.text
    register_body = res.json()
    assert register_body["token_type"] == "bearer"
    assert register_body["access_token"]
    assert register_body["user"]["email"] == email
    assert register_body["user"]["name"] == name

    # --- duplicate register should fail with the message the BFF surfaces ---
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "name": name},
    )
    assert res.status_code == 400
    assert "already registered" in res.json()["detail"].lower()

    # --- login ---
    res = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert res.status_code == 200, res.text
    login_body = res.json()
    assert login_body["access_token"]
    assert login_body["user"]["email"] == email

    token = login_body["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # --- /v1/me ---
    res = client.get("/v1/me", headers=auth)
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["email"] == email
    assert me["name"] == name

    # --- /v1/submissions (empty for a brand-new user) ---
    res = client.get("/v1/submissions", headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"items": []}


def test_login_with_wrong_password_returns_401(client: TestClient) -> None:
    email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": email, "password": "rightpw", "name": "X"},
    )
    res = client.post(
        "/v1/auth/login",
        json={"email": email, "password": "wrongpw"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_protected_route_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/v1/submissions")
    # FastAPI's HTTPBearer dependency returns 403 by default when the header
    # is missing, which is what the frontend's BFF treats as "unauthorized".
    assert res.status_code in (401, 403)


def test_health(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_job_listing_returns_runtime_storage_fields(client: TestClient) -> None:
    email = _unique_email()
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery-staple", "name": "Runtime Test"},
    )
    token = res.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    user_id = res.json()["user"]["id"]

    submission_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    dataset_id = str(uuid.uuid4())
    now = "2026-06-01T00:00:00+00:00"
    from vis_arena_server.db import connect

    with connect() as db:
        db.execute(
            "insert into submissions (id, owner_id, name, status, score, s3_key, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (submission_id, user_id, "runtime-storage", "succeeded", 0.75, "submissions/test.zip", now),
        )
        db.execute(
            """
            insert into jobs (
              id, submission_id, dataset_id, task_id, status, result_json,
              artifact_s3_prefix, preview_s3_key, generation_s3_prefix,
              evaluation_s3_prefix, agent_info_s3_key,
              generation_trajectory_s3_key, evaluation_trajectory_s3_key,
              evaluation_report_s3_key, started_at, completed_at,
              run_seconds, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                submission_id,
                dataset_id,
                "monthly-sales",
                "succeeded",
                json.dumps({"score": 0.75}),
                f"jobs/{job_id}/generation/artifacts",
                f"jobs/{job_id}/generation/preview/index.html",
                f"jobs/{job_id}/generation",
                f"jobs/{job_id}/evaluation",
                f"jobs/{job_id}/generation/agent-info.json",
                f"jobs/{job_id}/generation/trajectory.jsonl",
                f"jobs/{job_id}/evaluation/trajectory.jsonl",
                f"jobs/{job_id}/evaluation/report.json",
                "2026-06-01T00:00:01+00:00",
                "2026-06-01T00:00:04+00:00",
                3.0,
                now,
                now,
            ),
        )

    res = client.get(f"/v1/submissions/{submission_id}/jobs", headers=auth)
    assert res.status_code == 200, res.text
    item = res.json()["items"][0]
    assert item["generation_s3_prefix"] == f"jobs/{job_id}/generation"
    assert item["evaluation_s3_prefix"] == f"jobs/{job_id}/evaluation"
    assert item["agent_info_s3_key"] == f"jobs/{job_id}/generation/agent-info.json"
    assert item["generation_trajectory_s3_key"] == f"jobs/{job_id}/generation/trajectory.jsonl"
    assert item["evaluation_trajectory_s3_key"] == f"jobs/{job_id}/evaluation/trajectory.jsonl"
    assert item["evaluation_report_s3_key"] == f"jobs/{job_id}/evaluation/report.json"
    assert item["run_seconds"] == 3.0
    assert item["result"] == {"score": 0.75}
    assert "result_json" not in item


def test_upload_runtime_files_uses_phase_s3_layout(tmp_path: Path, monkeypatch) -> None:
    from vis_arena_server import evaluator

    reports_dir = tmp_path / "reports"
    work_dir = tmp_path / "work"
    (reports_dir / "generation").mkdir(parents=True)
    (reports_dir / "evaluation").mkdir(parents=True)
    work_dir.mkdir()

    (reports_dir / "generation" / "runtime.log").write_text("generation log", encoding="utf-8")
    (reports_dir / "generation" / "trajectory.jsonl").write_text('{"phase":"generation"}\n', encoding="utf-8")
    (work_dir / "agent-info.json").write_text('{"name":"agent"}', encoding="utf-8")
    (reports_dir / "evaluation" / "runtime.log").write_text("evaluation log", encoding="utf-8")
    (reports_dir / "evaluation" / "trajectory.jsonl").write_text('{"phase":"evaluation"}\n', encoding="utf-8")
    (reports_dir / "evaluation" / "report.json").write_text('{"score":1}', encoding="utf-8")

    uploads: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(evaluator, "upload_s3_file", lambda path, key, content_type: uploads.append((path, key, content_type)))

    result = evaluator.upload_runtime_files("job-123", reports_dir, work_dir)

    assert result == {
        "generation_s3_prefix": "jobs/job-123/generation",
        "evaluation_s3_prefix": "jobs/job-123/evaluation",
        "agent_info_s3_key": "jobs/job-123/generation/agent-info.json",
        "generation_trajectory_s3_key": "jobs/job-123/generation/trajectory.jsonl",
        "evaluation_trajectory_s3_key": "jobs/job-123/evaluation/trajectory.jsonl",
        "evaluation_report_s3_key": "jobs/job-123/evaluation/report.json",
    }
    assert [(key, content_type) for _path, key, content_type in uploads] == [
        ("jobs/job-123/generation/runtime.log", "text/plain"),
        ("jobs/job-123/generation/trajectory.jsonl", "application/x-ndjson"),
        ("jobs/job-123/generation/agent-info.json", "application/json"),
        ("jobs/job-123/evaluation/runtime.log", "text/plain"),
        ("jobs/job-123/evaluation/trajectory.jsonl", "application/x-ndjson"),
        ("jobs/job-123/evaluation/report.json", "application/json"),
    ]


def test_generation_artifacts_zip_excludes_task_data(tmp_path: Path) -> None:
    from vis_arena_server.evaluator import make_generation_artifacts_zip

    work_dir = tmp_path / "work"
    (work_dir / "task" / "data").mkdir(parents=True)
    (work_dir / "output" / "dist").mkdir(parents=True)
    (work_dir / "output" / "source").mkdir(parents=True)
    (work_dir / "task" / "data" / "large.csv").write_text("not an artifact", encoding="utf-8")
    (work_dir / "output" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (work_dir / "output" / "source" / "main.js").write_text("console.log('ok')", encoding="utf-8")
    target_zip = tmp_path / "artifacts.zip"

    make_generation_artifacts_zip(work_dir, target_zip)

    with ZipFile(target_zip) as archive:
        names = set(archive.namelist())
    assert names == {"dist/index.html", "source/main.js"}
