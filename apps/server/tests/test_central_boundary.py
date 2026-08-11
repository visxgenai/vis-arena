"""Central-judge boundary: no raw data in the container, fail-closed grading, no API leak."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vis_arena_server import evaluator
from vis_arena_server.db import connect, init_db, now_iso


@pytest.fixture(autouse=True)
def _clean() -> None:
    init_db()
    with connect() as db:
        for table in ("llm_usage", "evaluations", "round_participants", "jobs", "review_rounds", "submissions", "users"):
            db.execute(f"delete from {table}")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Finding 1: central containers must not see raw task data
# ---------------------------------------------------------------------------

def _run_evaluation_workspace(monkeypatch, job_type: str) -> list[str]:
    """Run run_peer_review_job with all IO faked; return the evaluate-workdir listing
    as seen at container-run time."""
    seen: list[str] = []

    def fake_copy_task_data(dataset_s3_key, task_id, staging_dir):
        task_root = Path(staging_dir) / "task"
        (task_root / "data").mkdir(parents=True)
        (task_root / "task.md").write_text("# task")
        (task_root / "data" / "secret.json").write_text('{"answers": "raw dataset"}')
        return task_root

    def fake_run_docker(root, job, *, phase, artifact_url=None):
        workdir = Path(root) / "work" / "evaluate"
        seen.extend(sorted(p.relative_to(workdir).as_posix() for p in workdir.rglob("*")))
        (workdir / "evaluation.json").write_text(json.dumps({"score": 1}))
        return {"returncode": 0, "log_tail": ""}

    monkeypatch.setattr(evaluator, "download_s3", lambda key, dest: dest.write_bytes(b""))
    monkeypatch.setattr(evaluator, "safe_extract_zip", lambda src, dest: dest.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(evaluator, "copy_sdk", lambda target: target.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(evaluator, "copy_task_data", fake_copy_task_data)
    monkeypatch.setattr(evaluator, "run_docker", fake_run_docker)
    monkeypatch.setattr(evaluator, "upload_runtime_files", lambda job_id, reports_dir, work_dir: {})

    job = {
        "id": _id("job"),
        "job_type": job_type,
        "review_target_job_id": _id("target"),
        "target_preview_s3_key": "jobs/x/generation/preview/index.html",
        "submission_s3_key": "submissions/x/submission.zip",
        "dataset_s3_key": "datasets/x/bundle.zip",
        "task_id": "some-task",
    }
    evaluator.run_peer_review_job(job, use_docker=True, update_intermediate_metadata=False)
    return seen


def test_central_evaluation_workspace_has_no_raw_data(monkeypatch) -> None:
    listing = _run_evaluation_workspace(monkeypatch, "central_evaluation")
    assert "task.md" in listing
    assert not any(entry.startswith("data") for entry in listing), listing


def test_peer_evaluation_workspace_keeps_task_data(monkeypatch) -> None:
    listing = _run_evaluation_workspace(monkeypatch, "peer_evaluation")
    assert "data/secret.json" in listing


# ---------------------------------------------------------------------------
# Finding 4: central grading errors must fail the job, not succeed unscored
# ---------------------------------------------------------------------------

def _insert_central_job() -> tuple[str, str]:
    now = now_iso()
    with connect() as db:
        for role in ("player", "judge"):
            db.execute(
                "insert into users (id, email, password_hash, name, created_at) values (?, ?, ?, ?, ?)",
                (f"{role}-u", f"{role}@example.com", "hash", role, now),
            )
            db.execute(
                "insert into submissions (id, owner_id, name, status, score, s3_key, finalized_at, created_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{role}-sub", f"{role}-u", role, "succeeded", None, f"submissions/{role}-sub/submission.zip", now, now),
            )
        job_id = _id("central")
        evaluation_id = _id("eval")
        artifact_job_id = _id("target")
        db.execute(
            "insert into jobs (id, submission_id, job_type, generator_submission_id, review_target_job_id, "
            "dataset_id, task_id, status, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, "judge-sub", "central_evaluation", "player-sub", artifact_job_id, "ds", "held-out-task", "running", now, now),
        )
        db.execute(
            "insert into evaluations (id, round_id, artifact_job_id, evaluator_type, evaluator_user_id, "
            "evaluator_submission_id, evaluator_name, job_id, status, created_at, updated_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (evaluation_id, "legacy", artifact_job_id, "central", "judge-u", "judge-sub", "Judge", job_id, "running", now, now),
        )
    return job_id, evaluation_id


def test_central_grading_failure_fails_the_job(monkeypatch) -> None:
    from vis_arena_server import judge_grading

    def broken_grading(task_id, raw_report):
        raise RuntimeError("answer key missing from S3")

    monkeypatch.setattr(judge_grading, "grade_central_result", broken_grading)
    job_id, evaluation_id = _insert_central_job()

    evaluator.complete_job(job_id, {"result": {"answers": [], "rubric": []}})

    with connect() as db:
        job = db.execute("select status, error from jobs where id = ?", (job_id,)).fetchone()
        evaluation = db.execute("select status, score from evaluations where id = ?", (evaluation_id,)).fetchone()
    assert job["status"] == "failed"
    assert "answer key missing" in (job["error"] or "")
    assert evaluation["status"] == "failed"
    assert evaluation["score"] is None


# ---------------------------------------------------------------------------
# Finding 5: central rows must not reach the submission-jobs API
# ---------------------------------------------------------------------------

def test_submission_jobs_api_hides_central_rows(client: TestClient) -> None:
    user_id, headers = _register(client)
    now = now_iso()
    submission_id = _id("sub")
    with connect() as db:
        db.execute(
            "insert into submissions (id, owner_id, name, status, score, s3_key, finalized_at, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (submission_id, user_id, "mine", "succeeded", None, f"submissions/{submission_id}/submission.zip", now, now),
        )
        db.execute(
            "insert into jobs (id, submission_id, job_type, generator_submission_id, dataset_id, task_id, "
            "status, result_json, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_id("central"), "other-judge-sub", "central_evaluation", submission_id, "ds", "held-out-task",
             "succeeded", json.dumps({"score": 123.0, "summary": "private"}), now, now),
        )
        db.execute(
            "insert into jobs (id, submission_id, job_type, generator_submission_id, dataset_id, task_id, "
            "status, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_id("peer"), "other-peer-sub", "peer_evaluation", submission_id, "ds", "public-task", "succeeded", now, now),
        )

    res = client.get(f"/v1/submissions/{submission_id}/jobs", headers=headers)
    assert res.status_code == 200, res.text
    job_types = [item["job_type"] for item in res.json()["items"]]
    assert "peer_evaluation" in job_types
    assert "central_evaluation" not in job_types
    assert "held-out-task" not in json.dumps(res.json())


def _register(client: TestClient) -> tuple[str, dict[str, str]]:
    email = f"{_id('user')}@example.com"
    res = client.post("/v1/auth/register", json={"email": email, "password": "password123", "name": email})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}
