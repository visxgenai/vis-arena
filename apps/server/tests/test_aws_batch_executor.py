from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vis_arena_server import evaluator, executor
from vis_arena_server.auth import create_job_token
from vis_arena_server.db import connect, init_db, now_iso
from vis_arena_server.settings import settings


@pytest.fixture(autouse=True)
def _clean_db():
    settings.executor_mode = executor.LOCAL_DOCKER
    init_db()
    with connect() as db:
        for table in ("llm_usage", "evaluations", "round_participants", "jobs", "review_rounds", "tasks", "submissions", "datasets", "users"):
            db.execute(f"delete from {table}")
    yield
    settings.executor_mode = executor.LOCAL_DOCKER


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _insert_job(*, status: str = "queued", executor_mode: str = executor.AWS_BATCH_FARGATE) -> tuple[str, str]:
    user_id = _id("user")
    submission_id = _id("sub")
    dataset_id = _id("dataset")
    task_id = _id("task")
    job_id = _id("job")
    now = now_iso()
    with connect() as db:
        db.execute(
            "insert into users (id, email, password_hash, name, created_at) values (?, ?, ?, ?, ?)",
            (user_id, f"{user_id}@example.com", "hash", user_id, now),
        )
        db.execute(
            "insert into datasets (id, owner_id, name, visibility, task_count, s3_key, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (dataset_id, user_id, dataset_id, "public", 1, f"datasets/{dataset_id}/bundle.zip", now),
        )
        db.execute(
            "insert into tasks (id, dataset_id, title, version, metadata_json, task_path) values (?, ?, ?, ?, ?, ?)",
            (task_id, dataset_id, task_id, 1, "{}", f"{task_id}/task.md"),
        )
        db.execute(
            """
            insert into submissions (
              id, owner_id, name, status, score, s3_key, finalized_at, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (submission_id, user_id, submission_id, "running", None, f"submissions/{submission_id}/submission.zip", now, now),
        )
        db.execute(
            """
            insert into jobs (
              id, submission_id, job_type, generator_submission_id, dataset_id,
              task_id, status, executor, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, submission_id, "generation", submission_id, dataset_id, task_id, status, executor_mode, now, now),
        )
    return job_id, submission_id


def test_aws_batch_dispatch_is_idempotent(monkeypatch) -> None:
    job_id, _ = _insert_job()
    settings.executor_mode = executor.AWS_BATCH_FARGATE
    settings.aws_batch_job_queue = "vis-arena-queue"
    settings.aws_batch_job_definition = "vis-arena-jobdef"
    calls: list[dict] = []

    class FakeBatch:
        def submit_job(self, **kwargs):
            calls.append(kwargs)
            return {"jobId": "batch-job-1"}

    monkeypatch.setattr(executor, "_batch_client", lambda: FakeBatch())

    first = executor.dispatch_queued_jobs()
    second = executor.dispatch_queued_jobs()

    assert len(calls) == 1
    assert first == [{"job_id": job_id, "executor": executor.AWS_BATCH_FARGATE, "external_job_id": "batch-job-1", "dispatched": True}]
    assert second == []
    with connect() as db:
        row = db.execute("select executor, external_job_id, dispatched_at, executor_error from jobs where id = ?", (job_id,)).fetchone()
    assert row["executor"] == executor.AWS_BATCH_FARGATE
    assert row["external_job_id"] == "batch-job-1"
    assert row["dispatched_at"]
    assert row["executor_error"] is None


def test_runner_lease_requires_job_scoped_token(client: TestClient) -> None:
    job_id, _ = _insert_job()

    res = client.get(f"/internal/jobs/{job_id}/lease")
    assert res.status_code == 401

    res = client.get(f"/internal/jobs/{job_id}/lease", headers={"Authorization": f"Bearer {create_job_token('other-job')}"})
    assert res.status_code == 401

    res = client.get(f"/internal/jobs/{job_id}/lease", headers={"Authorization": f"Bearer {create_job_token(job_id)}"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["job"]["id"] == job_id
    assert body["job"]["status"] == "running"
    assert body["job"]["submission_s3_key"]
    assert body["job"]["dataset_s3_key"]


def test_runner_complete_reuses_job_completion_logic(client: TestClient) -> None:
    job_id, submission_id = _insert_job(status="running")
    token = create_job_token(job_id)
    result = {
        "result": {"score": 0.82, "max_score": 1.0},
        "artifact_s3_prefix": f"jobs/{job_id}/generation/artifacts",
        "preview_s3_key": f"jobs/{job_id}/generation/preview/index.html",
        "generation_s3_prefix": f"jobs/{job_id}/generation",
        "evaluation_s3_prefix": f"jobs/{job_id}/evaluation",
        "agent_info_s3_key": f"jobs/{job_id}/generation/agent-info.json",
        "generation_trajectory_s3_key": None,
        "evaluation_trajectory_s3_key": None,
        "generation_agent_trajectory_s3_key": None,
        "evaluation_agent_trajectory_s3_key": None,
        "evaluation_report_s3_key": f"jobs/{job_id}/evaluation/report.json",
        "started_at": "2026-06-01T00:00:00+00:00",
        "completed_at": "2026-06-01T00:00:02+00:00",
        "run_seconds": 2.0,
        "generation_run_seconds": 1.25,
        "self_evaluation_run_seconds": 0.75,
    }

    res = client.post(f"/internal/jobs/{job_id}/complete", headers={"Authorization": f"Bearer {token}"}, json=result)
    assert res.status_code == 200, res.text

    with connect() as db:
        job = db.execute("select status, result_json, run_seconds from jobs where id = ?", (job_id,)).fetchone()
        submission = db.execute("select status from submissions where id = ?", (submission_id,)).fetchone()
        evaluation = db.execute("select status, score from evaluations where artifact_job_id = ? and evaluator_type = 'self'", (job_id,)).fetchone()

    assert job["status"] == "succeeded"
    assert json.loads(job["result_json"]) == {"score": 0.82, "max_score": 1.0}
    assert job["run_seconds"] == 2.0
    assert submission["status"] == "succeeded"
    assert dict(evaluation) == {"status": "succeeded", "score": 0.82}


def test_runner_complete_ignores_followup_dispatch_failure(client: TestClient, monkeypatch) -> None:
    job_id, submission_id = _insert_job(status="running")
    token = create_job_token(job_id)
    result = {
        "result": {"score": 0.82, "max_score": 1.0},
        "artifact_s3_prefix": f"jobs/{job_id}/generation/artifacts",
        "preview_s3_key": f"jobs/{job_id}/generation/preview/index.html",
        "generation_s3_prefix": f"jobs/{job_id}/generation",
        "evaluation_s3_prefix": f"jobs/{job_id}/evaluation",
        "agent_info_s3_key": f"jobs/{job_id}/generation/agent-info.json",
        "generation_trajectory_s3_key": None,
        "evaluation_trajectory_s3_key": None,
        "generation_agent_trajectory_s3_key": None,
        "evaluation_agent_trajectory_s3_key": None,
        "evaluation_report_s3_key": f"jobs/{job_id}/evaluation/report.json",
        "started_at": "2026-06-01T00:00:00+00:00",
        "completed_at": "2026-06-01T00:00:02+00:00",
        "run_seconds": 2.0,
        "generation_run_seconds": 1.25,
        "self_evaluation_run_seconds": 0.75,
    }

    def fail_dispatch():
        raise RuntimeError("batch submit is down")

    monkeypatch.setattr(evaluator, "dispatch_queued_jobs", fail_dispatch)

    res = client.post(f"/internal/jobs/{job_id}/complete", headers={"Authorization": f"Bearer {token}"}, json=result)
    assert res.status_code == 200, res.text

    with connect() as db:
        job = db.execute("select status, error from jobs where id = ?", (job_id,)).fetchone()
        submission = db.execute("select status from submissions where id = ?", (submission_id,)).fetchone()

    assert dict(job) == {"status": "succeeded", "error": None}
    assert submission["status"] == "succeeded"


def test_runner_fail_reuses_job_failure_logic(client: TestClient) -> None:
    job_id, submission_id = _insert_job(status="running")
    token = create_job_token(job_id)

    res = client.post(f"/internal/jobs/{job_id}/fail", headers={"Authorization": f"Bearer {token}"}, json={"error": "boom"})
    assert res.status_code == 200, res.text

    with connect() as db:
        job = db.execute("select status, error from jobs where id = ?", (job_id,)).fetchone()
        submission = db.execute("select status from submissions where id = ?", (submission_id,)).fetchone()

    assert dict(job) == {"status": "failed", "error": "boom"}
    assert submission["status"] == "failed"


def test_reconcile_failed_batch_job_marks_backend_job_failed(monkeypatch) -> None:
    job_id, submission_id = _insert_job(status="running")
    settings.executor_mode = executor.AWS_BATCH_FARGATE
    with connect() as db:
        db.execute(
            "update jobs set executor = ?, external_job_id = ? where id = ?",
            (executor.AWS_BATCH_FARGATE, "batch-job-failed", job_id),
        )

    class FakeBatch:
        def describe_jobs(self, jobs):
            assert jobs == ["batch-job-failed"]
            return {"jobs": [{"jobId": "batch-job-failed", "status": "FAILED", "statusReason": "Task timed out"}]}

    monkeypatch.setattr(executor, "_batch_client", lambda: FakeBatch())

    updates = executor.reconcile_batch_jobs()

    assert updates == [
        {
            "job_id": job_id,
            "external_job_id": "batch-job-failed",
            "status": "FAILED",
            "error": "Task timed out",
        }
    ]
    with connect() as db:
        job = db.execute("select status, error from jobs where id = ?", (job_id,)).fetchone()
        submission = db.execute("select status from submissions where id = ?", (submission_id,)).fetchone()
    assert dict(job) == {"status": "failed", "error": "Task timed out"}
    assert submission["status"] == "failed"


def test_direct_generation_publishes_preview_metadata_before_self_evaluation(tmp_path: Path, monkeypatch) -> None:
    job_id, _submission_id = _insert_job(status="running")
    phases: list[str] = []

    def fake_safe_extract(_zip_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "agent.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def fake_copy_task_data(_dataset_key: str, _task_id: str, target: Path) -> Path:
        task = target / "task"
        (task / "data").mkdir(parents=True)
        (task / "task.md").write_text("Task", encoding="utf-8")
        return task

    def fake_run_direct(root: Path, job: dict, *, phase: str, artifact_url: str | None = None) -> dict:
        phases.append(phase)
        if phase == "generation":
            assert artifact_url is None
            (root / "work" / "generate" / "source").mkdir(parents=True)
            (root / "work" / "generate" / "dist").mkdir(parents=True)
            (root / "work" / "generate" / "source" / "main.js").write_text("console.log('ok')", encoding="utf-8")
            (root / "work" / "generate" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
        else:
            assert phase == "evaluation"
            assert artifact_url == f"http://host.docker.internal:8000/v1/jobs/{job_id}/preview"
            with connect() as db:
                row = db.execute("select preview_s3_key from jobs where id = ?", (job_id,)).fetchone()
            assert row["preview_s3_key"] == f"jobs/{job_id}/generation/preview/index.html"
            (root / "work" / "evaluate" / "evaluation.json").write_text(json.dumps({"score": 91}), encoding="utf-8")
        return {
            "started_at": "2026-06-01T00:00:00+00:00",
            "completed_at": "2026-06-01T00:00:01+00:00",
            "run_seconds": 1.0,
            "returncode": 0,
            "log_tail": "",
        }

    monkeypatch.setattr(evaluator, "download_s3", lambda _key, target: target.write_bytes(b"zip"))
    monkeypatch.setattr(evaluator, "safe_extract_zip", fake_safe_extract)
    monkeypatch.setattr(evaluator, "copy_sdk", lambda _target: None)
    monkeypatch.setattr(evaluator, "copy_task_data", fake_copy_task_data)
    monkeypatch.setattr(evaluator, "run_direct", fake_run_direct)
    monkeypatch.setattr(evaluator, "upload_s3_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evaluator, "upload_s3_directory", lambda *_args, **_kwargs: None)

    result = evaluator.run_generation_job(
        {
            "id": job_id,
            "job_type": "generation",
            "submission_s3_key": "submissions/sub/submission.zip",
            "dataset_s3_key": "datasets/ds/bundle.zip",
            "task_id": "task-1",
        },
        use_docker=False,
        update_intermediate_metadata=False,
    )

    assert result["result"] == {"score": 91}
    assert phases == ["generation", "evaluation"]


def test_copy_sdk_uses_configured_repo_root(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    sdk_root = repo_root / "packages" / "arena-sdk"
    sdk_root.mkdir(parents=True)
    (sdk_root / "pyproject.toml").write_text("[project]\nname = \"arena-sdk-test\"\n", encoding="utf-8")
    target = tmp_path / "target-sdk"

    monkeypatch.setenv("VIS_ARENA_REPO_ROOT", str(repo_root))

    evaluator.copy_sdk(target)

    assert (target / "pyproject.toml").read_text(encoding="utf-8") == "[project]\nname = \"arena-sdk-test\"\n"
    shutil.rmtree(target)
