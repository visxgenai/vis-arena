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

    # --- PATCH /v1/me ---
    res = client.patch("/v1/me", headers=auth, json={"name": "Updated Smoke User"})
    assert res.status_code == 200, res.text
    updated = res.json()
    assert updated["email"] == email
    assert updated["name"] == "Updated Smoke User"

    res = client.get("/v1/me", headers=auth)
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Updated Smoke User"

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
              generation_agent_trajectory_s3_key, evaluation_agent_trajectory_s3_key,
              evaluation_report_s3_key, started_at, completed_at,
              run_seconds, generation_run_seconds, self_evaluation_run_seconds,
              created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                f"jobs/{job_id}/generation/agent-trajectory.jsonl",
                f"jobs/{job_id}/evaluation/agent-trajectory.jsonl",
                f"jobs/{job_id}/evaluation/report.json",
                "2026-06-01T00:00:01+00:00",
                "2026-06-01T00:00:04+00:00",
                3.0,
                2.0,
                1.0,
                now,
                now,
            ),
        )
        for purpose, input_tokens, output_tokens in (
            ("generation", 100, 25),
            ("evaluation", 40, 10),
        ):
            db.execute(
                """
                insert into llm_usage (
                  id, job_id, submission_id, user_id, provider, model_id, purpose,
                  input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                  latency_ms, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    submission_id,
                    user_id,
                    "bedrock",
                    "model",
                    purpose,
                    input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    None,
                    25,
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
    assert item["generation_agent_trajectory_s3_key"] == f"jobs/{job_id}/generation/agent-trajectory.jsonl"
    assert item["evaluation_agent_trajectory_s3_key"] == f"jobs/{job_id}/evaluation/agent-trajectory.jsonl"
    assert item["evaluation_report_s3_key"] == f"jobs/{job_id}/evaluation/report.json"
    assert item["run_seconds"] == 3.0
    assert item["generation_run_seconds"] == 2.0
    assert item["self_evaluation_run_seconds"] == 1.0
    assert item["usage"]["total_tokens"] == 175
    assert item["generation_usage"]["total_tokens"] == 125
    assert item["self_evaluation_usage"]["total_tokens"] == 50
    assert item["usage_by_purpose"]["generation"]["request_count"] == 1
    assert item["usage_by_purpose"]["evaluation"]["request_count"] == 1
    assert item["result"] == {"score": 0.75}
    assert "result_json" not in item


def test_upload_runtime_files_uses_phase_s3_layout(tmp_path: Path, monkeypatch) -> None:
    from vis_arena_server import evaluator
    from vis_arena_server.trajectory import broker_trajectory_path

    reports_dir = tmp_path / "reports"
    work_dir = tmp_path / "work"
    (reports_dir / "generation").mkdir(parents=True)
    (reports_dir / "evaluation").mkdir(parents=True)
    (work_dir / "evaluate").mkdir(parents=True)

    (reports_dir / "generation" / "runtime.log").write_text("generation log", encoding="utf-8")
    (reports_dir / "generation" / "trajectory.jsonl").write_text('{"phase":"generation"}\n', encoding="utf-8")
    (reports_dir / "generation" / "agent-info.json").write_text('{"name":"agent"}', encoding="utf-8")
    (reports_dir / "evaluation" / "runtime.log").write_text("evaluation log", encoding="utf-8")
    (reports_dir / "evaluation" / "trajectory.jsonl").write_text('{"phase":"evaluation"}\n', encoding="utf-8")
    (work_dir / "evaluate" / "evaluation.json").write_text('{"score":1}', encoding="utf-8")
    generation_agent_trajectory = broker_trajectory_path("job-123", "generation")
    evaluation_agent_trajectory = broker_trajectory_path("job-123", "evaluation")
    generation_agent_trajectory.parent.mkdir(parents=True, exist_ok=True)
    generation_agent_trajectory.write_text('{"type":"tool_call"}\n', encoding="utf-8")
    evaluation_agent_trajectory.write_text('{"type":"tool_response"}\n', encoding="utf-8")

    uploads: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(evaluator, "upload_s3_file", lambda path, key, content_type: uploads.append((path, key, content_type)))

    result = evaluator.upload_runtime_files("job-123", reports_dir, work_dir)

    assert result == {
        "generation_s3_prefix": "jobs/job-123/generation",
        "evaluation_s3_prefix": "jobs/job-123/evaluation",
        "agent_info_s3_key": "jobs/job-123/generation/agent-info.json",
        "generation_trajectory_s3_key": "jobs/job-123/generation/trajectory.jsonl",
        "evaluation_trajectory_s3_key": "jobs/job-123/evaluation/trajectory.jsonl",
        "generation_agent_trajectory_s3_key": "jobs/job-123/generation/agent-trajectory.jsonl",
        "evaluation_agent_trajectory_s3_key": "jobs/job-123/evaluation/agent-trajectory.jsonl",
        "evaluation_report_s3_key": "jobs/job-123/evaluation/report.json",
    }
    assert [(key, content_type) for _path, key, content_type in uploads] == [
        ("jobs/job-123/generation/runtime.log", "text/plain"),
        ("jobs/job-123/generation/trajectory.jsonl", "application/x-ndjson"),
        ("jobs/job-123/generation/agent-trajectory.jsonl", "application/x-ndjson"),
        ("jobs/job-123/generation/agent-info.json", "application/json"),
        ("jobs/job-123/evaluation/runtime.log", "text/plain"),
        ("jobs/job-123/evaluation/trajectory.jsonl", "application/x-ndjson"),
        ("jobs/job-123/evaluation/agent-trajectory.jsonl", "application/x-ndjson"),
        ("jobs/job-123/evaluation/report.json", "application/json"),
    ]


def test_broker_trajectory_records_tool_calls_and_responses() -> None:
    from vis_arena_server.llm import _record_llm_request_trajectory, _record_llm_response_trajectory
    from vis_arena_server.schemas import LLMMessageRequest
    from vis_arena_server.trajectory import broker_trajectory_path

    job_id = f"job-{uuid.uuid4()}"
    payload = LLMMessageRequest(
        job_id=job_id,
        purpose="generation",
        model="model-a",
        tools=[{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}],
        messages=[
            {"role": "user", "content": "make a chart"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "file.csv\n"},
        ],
    )
    context = {"submission_id": "submission-1"}

    _record_llm_request_trajectory(payload, context, "model-a", 4096, 1000000)
    _record_llm_response_trajectory(
        payload,
        {
            "model": "model-a",
            "message": {
                "role": "assistant",
                "content": "I will inspect the data.",
                "tool_calls": [
                    {"id": "call-2", "type": "function", "function": {"name": "bash", "arguments": '{"command":"cat file.csv"}'}}
                ],
            },
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        },
        123,
    )

    events = [json.loads(line) for line in broker_trajectory_path(job_id, "generation").read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in events] == ["llm_request", "tool_response", "llm_response", "tool_call"]
    assert events[1]["tool"] == "bash"
    assert events[1]["content_preview"] == "file.csv\n"
    assert events[3]["tool"] == "bash"
    assert events[3]["arguments"] == {"command": "cat file.csv"}


def test_generation_artifacts_zip_excludes_task_data(tmp_path: Path) -> None:
    from vis_arena_server.evaluator import make_generation_artifacts_zip

    # The post-refactor generate workdir holds inputs (task.md, data/) alongside
    # the agent's outputs (source/, dist/, generation.json). The artifact zip
    # should ship only the outputs.
    work_dir = tmp_path / "work"
    generate = work_dir / "generate"
    (generate / "data").mkdir(parents=True)
    (generate / "dist").mkdir(parents=True)
    (generate / "source").mkdir(parents=True)
    (generate / "task.md").write_text("# task", encoding="utf-8")
    (generate / "data" / "large.csv").write_text("not an artifact", encoding="utf-8")
    (generate / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (generate / "source" / "main.js").write_text("console.log('ok')", encoding="utf-8")
    target_zip = tmp_path / "artifacts.zip"

    make_generation_artifacts_zip(work_dir, target_zip)

    with ZipFile(target_zip) as archive:
        names = set(archive.namelist())
    assert names == {"dist/index.html", "source/main.js"}


def test_container_script_supports_requirements_txt() -> None:
    from vis_arena_server.evaluator import render_container_script

    script = render_container_script("generation")
    # requirements.txt path installs deps via uv for all three steps (info, generate, evaluate)
    assert script.count("uv run --with-requirements requirements.txt --with-editable /arena/sdk") == 3
    assert "if [ -f requirements.txt ]; then" in script
    # requirements.txt takes precedence over pyproject.toml
    assert script.index("requirements.txt") < script.index("pyproject.toml")
    # the executable ./agent fallback is still present
    assert "./agent generate" in script


def test_login_token_outlasts_event_while_job_token_stays_short() -> None:
    from datetime import UTC, datetime

    import jwt
    from vis_arena_server.auth import LOGIN_TOKEN_DAYS, create_token
    from vis_arena_server.settings import settings

    login = jwt.decode(create_token("u1", expires_days=LOGIN_TOKEN_DAYS), settings.secret_key, algorithms=["HS256"])
    job = jwt.decode(create_token("u1"), settings.secret_key, algorithms=["HS256"])  # default (per-job)
    login_days = (datetime.fromtimestamp(login["exp"], UTC) - datetime.now(UTC)).days
    job_days = (datetime.fromtimestamp(job["exp"], UTC) - datetime.now(UTC)).days
    assert login_days >= 100        # comfortably past the event window
    assert 25 <= job_days <= 31     # per-job token stays short (default 30d)
