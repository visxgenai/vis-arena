from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from vis_arena_server import evaluator, storage
from vis_arena_server.db import connect, init_db, now_iso


@pytest.fixture(autouse=True)
def _clean_db() -> None:
    init_db()
    with connect() as db:
        for table in ("llm_usage", "jobs", "tasks", "submissions", "datasets", "users"):
            db.execute(f"delete from {table}")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _register(client: TestClient) -> tuple[str, dict[str, str]]:
    email = f"{_id('user')}@example.com"
    res = client.post("/v1/auth/register", json={"email": email, "password": "password123", "name": email})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


def _insert_user(user_id: str | None = None) -> str:
    user_id = user_id or _id("user")
    with connect() as db:
        db.execute(
            "insert into users (id, email, password_hash, name, created_at) values (?, ?, ?, ?, ?)",
            (user_id, f"{user_id}@example.com", "hash", user_id, now_iso()),
        )
    return user_id


def _insert_dataset(task_count: int = 1, visibility: str = "public") -> tuple[str, list[str]]:
    dataset_id = _id("dataset")
    task_ids = [_id("task") for _ in range(task_count)]
    with connect() as db:
        db.execute(
            "insert into datasets (id, owner_id, name, visibility, task_count, s3_key, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (dataset_id, _id("owner"), dataset_id, visibility, task_count, f"datasets/{dataset_id}/bundle.zip", now_iso()),
        )
        for task_id in task_ids:
            db.execute(
                "insert into tasks (id, dataset_id, title, version, metadata_json, task_path) values (?, ?, ?, ?, ?, ?)",
                (task_id, dataset_id, task_id, 1, "{}", f"{task_id}/task.md"),
            )
    return dataset_id, task_ids


def _insert_submission(
    owner_id: str,
    *,
    status: str = "queued",
    finalized_at: str | None = None,
    reviewer_eligible_at: str | None = None,
    created_at: str | None = None,
) -> str:
    submission_id = _id("sub")
    created_at = created_at or now_iso()
    with connect() as db:
        db.execute(
            """
            insert into submissions (
              id, owner_id, name, status, score, s3_key,
              finalized_at, reviewer_eligible_at, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                owner_id,
                submission_id,
                status,
                None,
                f"submissions/{submission_id}/submission.zip",
                finalized_at,
                reviewer_eligible_at,
                created_at,
            ),
        )
    return submission_id


def _insert_generation_job(
    submission_id: str,
    dataset_id: str,
    task_id: str,
    *,
    status: str = "queued",
    artifact_s3_prefix: str | None = None,
) -> str:
    job_id = _id("job")
    with connect() as db:
        db.execute(
            """
            insert into jobs (
              id, submission_id, job_type, generator_submission_id,
              dataset_id, task_id, status, artifact_s3_prefix,
              created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                submission_id,
                "generation",
                submission_id,
                dataset_id,
                task_id,
                status,
                artifact_s3_prefix,
                now_iso(),
                now_iso(),
            ),
        )
    return job_id


def _complete_generation(job_id: str) -> None:
    evaluator.complete_job(
        job_id,
        {
            "result": {"artifact_s3_prefix": f"jobs/{job_id}/generation/artifacts", "preview_s3_key": f"jobs/{job_id}/generation/preview/index.html"},
            "artifact_s3_prefix": f"jobs/{job_id}/generation/artifacts",
            "preview_s3_key": f"jobs/{job_id}/generation/preview/index.html",
            "generation_s3_prefix": f"jobs/{job_id}/generation",
            "evaluation_s3_prefix": f"jobs/{job_id}/evaluation",
            "agent_info_s3_key": f"jobs/{job_id}/generation/agent-info.json",
            "generation_trajectory_s3_key": None,
            "evaluation_trajectory_s3_key": None,
            "evaluation_report_s3_key": None,
            "started_at": "2026-06-01T00:00:00+00:00",
            "completed_at": "2026-06-01T00:00:01+00:00",
            "run_seconds": 1.0,
        },
    )


def test_submission_upload_rate_limit_is_per_user_per_utc_day(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(storage, "presigned_put", lambda key: {"url": f"https://upload/{key}", "method": "PUT", "headers": {}, "expires_in": 1})
    _user_id, auth = _register(client)
    _other_user_id, other_auth = _register(client)

    for index in range(3):
        res = client.post("/v1/submissions/uploads", headers=auth, json={"name": f"sub-{index}"})
        assert res.status_code == 200, res.text

    res = client.post("/v1/submissions/uploads", headers=auth, json={"name": "sub-4"})
    assert res.status_code == 429

    res = client.post("/v1/submissions/uploads", headers=other_auth, json={"name": "other"})
    assert res.status_code == 200, res.text


def test_finalize_queues_generation_jobs_for_public_dataset_tasks(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    owner_id, auth = _register(client)
    public_dataset, public_tasks = _insert_dataset(task_count=2, visibility="public")
    private_dataset, _private_tasks = _insert_dataset(task_count=1, visibility="private")
    submission_id = _insert_submission(owner_id, status="uploading")

    bundle = tmp_path / "submission.zip"
    with ZipFile(bundle, "w") as archive:
        archive.writestr("agent.py", "#!/usr/bin/env python3\n")

    monkeypatch.setattr(storage, "download_s3", lambda _key, target: shutil.copyfile(bundle, target))

    res = client.post(f"/v1/submissions/{submission_id}/finalize", headers=auth, json={"dataset_id": private_dataset})
    assert res.status_code == 200, res.text

    with connect() as db:
        rows = db.execute("select job_type, generator_submission_id, dataset_id, task_id, status from jobs where submission_id = ?", (submission_id,)).fetchall()
        submission = db.execute("select finalized_at, reviewer_eligible_at from submissions where id = ?", (submission_id,)).fetchone()

    assert submission["finalized_at"]
    assert submission["reviewer_eligible_at"] is None
    assert {row["dataset_id"] for row in rows} == {public_dataset}
    assert {row["task_id"] for row in rows} == set(public_tasks)
    assert {row["job_type"] for row in rows} == {"generation"}
    assert {row["generator_submission_id"] for row in rows} == {submission_id}
    assert {row["status"] for row in rows} == {"queued"}


def test_finalize_rejects_retry_without_duplicate_jobs(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    owner_id, auth = _register(client)
    _dataset_id, public_tasks = _insert_dataset(task_count=2, visibility="public")
    submission_id = _insert_submission(owner_id, status="uploading")

    bundle = tmp_path / "submission.zip"
    with ZipFile(bundle, "w") as archive:
        archive.writestr("agent.py", "#!/usr/bin/env python3\n")

    monkeypatch.setattr(storage, "download_s3", lambda _key, target: shutil.copyfile(bundle, target))

    first = client.post(f"/v1/submissions/{submission_id}/finalize", headers=auth, json={})
    retry = client.post(f"/v1/submissions/{submission_id}/finalize", headers=auth, json={})

    assert first.status_code == 200, first.text
    assert retry.status_code == 409
    with connect() as db:
        job_count = db.execute("select count(*) as count from jobs where submission_id = ?", (submission_id,)).fetchone()["count"]

    assert job_count == len(public_tasks)


def test_finalize_fails_when_no_active_public_dataset(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    owner_id, auth = _register(client)
    _insert_dataset(task_count=1, visibility="private")
    submission_id = _insert_submission(owner_id, status="uploading")
    bundle = tmp_path / "submission.zip"
    with ZipFile(bundle, "w") as archive:
        archive.writestr("agent.py", "#!/usr/bin/env python3\n")
    monkeypatch.setattr(storage, "download_s3", lambda _key, target: shutil.copyfile(bundle, target))

    res = client.post(f"/v1/submissions/{submission_id}/finalize", headers=auth, json={"dataset_id": "anything"})

    assert res.status_code == 400


def test_submission_jobs_api_includes_generation_and_peer_reviews(client: TestClient) -> None:
    owner_id, auth = _register(client)
    reviewer_owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    submission_id = _insert_submission(owner_id, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    reviewer_submission = _insert_submission(reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:01:00+00:00", reviewer_eligible_at="2026-06-01T00:02:00+00:00")
    generation_job = _insert_generation_job(submission_id, dataset_id, task_id, status="succeeded", artifact_s3_prefix="jobs/gen/generation/artifacts")
    review_job = _id("review")
    with connect() as db:
        db.execute(
            """
            insert into jobs (
              id, submission_id, job_type, generator_submission_id,
              review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
              dataset_id, task_id, status, result_json, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_job,
                reviewer_submission,
                "peer_review",
                submission_id,
                generation_job,
                reviewer_owner,
                "2026-06-01T00:03:00+00:00",
                dataset_id,
                task_id,
                "succeeded",
                json.dumps({"score": 91}),
                "2026-06-01T00:03:00+00:00",
                "2026-06-01T00:04:00+00:00",
            ),
        )

    res = client.get(f"/v1/submissions/{submission_id}/jobs", headers=auth)

    assert res.status_code == 200, res.text
    items = {item["id"]: item for item in res.json()["items"]}
    assert items[generation_job]["job_type"] == "generation"
    assert items[review_job]["job_type"] == "peer_review"
    assert items[review_job]["submission_id"] == reviewer_submission
    assert items[review_job]["generator_submission_id"] == submission_id
    assert items[review_job]["review_target_job_id"] == generation_job
    assert items[review_job]["reviewer_user_id"] == reviewer_owner
    assert items[review_job]["score"] == 91


def test_llm_usage_is_attributed_to_consuming_submission(client: TestClient) -> None:
    generator_owner, generator_auth = _register(client)
    reviewer_owner, reviewer_auth = _register(client)
    dataset_id, (task_id,) = _insert_dataset()
    generator_submission = _insert_submission(generator_owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    reviewer_submission = _insert_submission(reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:01:00+00:00", reviewer_eligible_at="2026-06-01T00:02:00+00:00")
    generation_job = _insert_generation_job(generator_submission, dataset_id, task_id, status="succeeded", artifact_s3_prefix="jobs/gen/generation/artifacts")
    review_job = _id("review")
    with connect() as db:
        db.execute(
            """
            insert into jobs (
              id, submission_id, job_type, generator_submission_id,
              review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
              dataset_id, task_id, status, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_job,
                reviewer_submission,
                "peer_review",
                generator_submission,
                generation_job,
                reviewer_owner,
                "2026-06-01T00:03:00+00:00",
                dataset_id,
                task_id,
                "succeeded",
                "2026-06-01T00:03:00+00:00",
                "2026-06-01T00:04:00+00:00",
            ),
        )
        for job_id, submission_id, user_id, total_tokens in (
            (generation_job, generator_submission, generator_owner, 10),
            (review_job, reviewer_submission, reviewer_owner, 20),
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
                    _id("usage"),
                    job_id,
                    submission_id,
                    user_id,
                    "bedrock",
                    "model",
                    "generate",
                    total_tokens // 2,
                    total_tokens // 2,
                    total_tokens,
                    None,
                    1,
                    "2026-06-01T00:05:00+00:00",
                ),
            )

    generator_usage = client.get(f"/v1/submissions/{generator_submission}/llm-usage", headers=generator_auth)
    reviewer_usage = client.get(f"/v1/submissions/{reviewer_submission}/llm-usage", headers=reviewer_auth)

    assert generator_usage.status_code == 200, generator_usage.text
    assert reviewer_usage.status_code == 200, reviewer_usage.text
    assert generator_usage.json()["summary"]["total_tokens"] == 10
    assert [job["job_id"] for job in generator_usage.json()["jobs"]] == [generation_job]
    assert reviewer_usage.json()["summary"]["total_tokens"] == 20
    assert [job["job_id"] for job in reviewer_usage.json()["jobs"]] == [review_job]


def test_peer_review_selection_uses_latest_other_user_submission_and_excludes_same_owner() -> None:
    generator_owner = _insert_user()
    reviewer_owner = _insert_user()
    second_reviewer_owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    generator_submission = _insert_submission(generator_owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    _same_owner_submission = _insert_submission(generator_owner, status="succeeded", finalized_at="2026-06-01T00:02:00+00:00", reviewer_eligible_at="2026-06-01T00:03:00+00:00")
    old_reviewer = _insert_submission(reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:01:00+00:00", reviewer_eligible_at="2026-06-01T00:02:00+00:00")
    latest_reviewer = _insert_submission(reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:03:00+00:00", reviewer_eligible_at="2026-06-01T00:04:00+00:00")
    second_reviewer = _insert_submission(second_reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:01:00+00:00", reviewer_eligible_at="2026-06-01T00:02:00+00:00")
    job_id = _insert_generation_job(generator_submission, dataset_id, task_id, status="running")

    _complete_generation(job_id)

    with connect() as db:
        reviews = db.execute("select submission_id, reviewer_user_id, status from jobs where job_type = 'peer_review' and review_target_job_id = ?", (job_id,)).fetchall()

    assert {(row["submission_id"], row["reviewer_user_id"], row["status"]) for row in reviews} == {
        (latest_reviewer, reviewer_owner, "queued"),
        (second_reviewer, second_reviewer_owner, "queued"),
    }
    assert old_reviewer not in {row["submission_id"] for row in reviews}


def test_waiting_reviewer_moves_to_queued_when_submission_becomes_eligible() -> None:
    generator_owner = _insert_user()
    reviewer_owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    generator_submission = _insert_submission(generator_owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    reviewer_submission = _insert_submission(reviewer_owner, status="running", finalized_at="2026-06-01T00:01:00+00:00")
    target_job = _insert_generation_job(generator_submission, dataset_id, task_id, status="running")

    _complete_generation(target_job)

    with connect() as db:
        review = db.execute("select id, submission_id, status from jobs where job_type = 'peer_review' and review_target_job_id = ?", (target_job,)).fetchone()
    assert review["submission_id"] == reviewer_submission
    assert review["status"] == "waiting_reviewer"

    reviewer_generation = _insert_generation_job(reviewer_submission, dataset_id, task_id, status="succeeded")
    with connect() as db:
        evaluator._update_generator_submission_rollup(db, reviewer_submission, now_iso())
        updated = db.execute("select status from jobs where id = ?", (review["id"],)).fetchone()

    assert updated["status"] == "queued"
    assert reviewer_generation


def test_failed_latest_reviewer_falls_back_to_previous_eligible_submission() -> None:
    generator_owner = _insert_user()
    reviewer_owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    generator_submission = _insert_submission(generator_owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    previous_reviewer = _insert_submission(reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:01:00+00:00", reviewer_eligible_at="2026-06-01T00:02:00+00:00")
    latest_reviewer = _insert_submission(reviewer_owner, status="running", finalized_at="2026-06-01T00:03:00+00:00")
    target_job = _insert_generation_job(generator_submission, dataset_id, task_id, status="running")
    _complete_generation(target_job)

    latest_generation = _insert_generation_job(latest_reviewer, dataset_id, task_id, status="failed")
    with connect() as db:
        evaluator._update_generator_submission_rollup(db, latest_reviewer, now_iso())
        review = db.execute("select submission_id, status from jobs where job_type = 'peer_review' and review_target_job_id = ?", (target_job,)).fetchone()

    assert review["submission_id"] == previous_reviewer
    assert review["status"] == "queued"
    assert latest_generation


def test_new_submission_does_not_backfill_existing_artifact_reviews() -> None:
    generator_owner = _insert_user()
    reviewer_owner = _insert_user()
    late_owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    generator_submission = _insert_submission(generator_owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    reviewer_submission = _insert_submission(reviewer_owner, status="succeeded", finalized_at="2026-06-01T00:01:00+00:00", reviewer_eligible_at="2026-06-01T00:02:00+00:00")
    target_job = _insert_generation_job(generator_submission, dataset_id, task_id, status="running")
    _complete_generation(target_job)

    _late_submission = _insert_submission(late_owner, status="succeeded", finalized_at="2026-06-01T00:10:00+00:00", reviewer_eligible_at="2026-06-01T00:11:00+00:00")
    evaluator.refresh_waiting_reviews()

    with connect() as db:
        reviews = db.execute("select submission_id from jobs where job_type = 'peer_review' and review_target_job_id = ?", (target_job,)).fetchall()

    assert [row["submission_id"] for row in reviews] == [reviewer_submission]


def test_rollup_averages_successful_peer_reviews_and_ignores_failures() -> None:
    owner = _insert_user()
    reviewer_owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    submission = _insert_submission(owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    generation = _insert_generation_job(submission, dataset_id, task_id, status="succeeded")
    with connect() as db:
        for score in (80, 100):
            db.execute(
                """
                insert into jobs (
                  id, submission_id, job_type, generator_submission_id,
                  review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
                  dataset_id, task_id, status, result_json, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_id("review"), _id("reviewer-sub"), "peer_review", submission, generation, reviewer_owner, now_iso(), dataset_id, task_id, "succeeded", json.dumps({"score": score}), now_iso(), now_iso()),
            )
        db.execute(
            """
            insert into jobs (
              id, submission_id, job_type, generator_submission_id,
              review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
              dataset_id, task_id, status, error, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_id("review"), _id("reviewer-sub"), "peer_review", submission, generation, reviewer_owner, now_iso(), dataset_id, task_id, "failed", "bad review", now_iso(), now_iso()),
        )
        evaluator._update_generator_submission_rollup(db, submission, now_iso())
        row = db.execute("select status, score from submissions where id = ?", (submission,)).fetchone()

    assert row["status"] == "succeeded"
    assert row["score"] == 90


def test_rollup_succeeds_with_null_score_when_no_peer_review_succeeds() -> None:
    owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    submission = _insert_submission(owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    _insert_generation_job(submission, dataset_id, task_id, status="succeeded")

    with connect() as db:
        evaluator._update_generator_submission_rollup(db, submission, now_iso())
        row = db.execute("select status, score from submissions where id = ?", (submission,)).fetchone()

    assert row["status"] == "succeeded"
    assert row["score"] is None


def test_rollup_fails_submission_when_all_generation_jobs_fail() -> None:
    owner = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    submission = _insert_submission(owner, status="running", finalized_at="2026-06-01T00:00:00+00:00")
    _insert_generation_job(submission, dataset_id, task_id, status="failed")

    with connect() as db:
        evaluator._update_generator_submission_rollup(db, submission, now_iso())
        row = db.execute("select status, score, reviewer_eligible_at from submissions where id = ?", (submission,)).fetchone()

    assert row["status"] == "failed"
    assert row["score"] is None
    assert row["reviewer_eligible_at"] is None


def test_worker_generation_uploads_artifact_zip_and_preview(tmp_path: Path, monkeypatch) -> None:
    uploads: list[tuple[str, str]] = []
    preview_uploads: list[str] = []

    def fake_safe_extract(_zip_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "agent.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def fake_copy_task_data(_dataset_key: str, _task_id: str, target: Path) -> Path:
        task = target / "task"
        (task / "data").mkdir(parents=True)
        (task / "task.md").write_text("Task", encoding="utf-8")
        return task

    def fake_run_docker(root: Path, job: dict) -> dict:
        assert (job.get("job_type") or "generation") == "generation"
        (root / "work" / "output" / "source").mkdir(parents=True)
        (root / "work" / "output" / "dist").mkdir(parents=True)
        (root / "work" / "output" / "source" / "main.js").write_text("console.log('ok')", encoding="utf-8")
        (root / "work" / "output" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
        (root / "work" / "agent-info.json").write_text("{}", encoding="utf-8")
        (root / "reports" / "generation").mkdir(parents=True)
        (root / "reports" / "generation" / "runtime.log").write_text("generation", encoding="utf-8")
        return {
            "started_at": "2026-06-01T00:00:00+00:00",
            "completed_at": "2026-06-01T00:00:01+00:00",
            "run_seconds": 1.0,
            "returncode": 0,
            "log_tail": "",
        }

    monkeypatch.setattr(evaluator, "download_s3", lambda _key, _target: None)
    monkeypatch.setattr(evaluator, "safe_extract_zip", fake_safe_extract)
    monkeypatch.setattr(evaluator, "copy_sdk", lambda _target: None)
    monkeypatch.setattr(evaluator, "copy_task_data", fake_copy_task_data)
    monkeypatch.setattr(evaluator, "run_docker", fake_run_docker)
    monkeypatch.setattr(evaluator, "update_job_runtime_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evaluator, "upload_s3_file", lambda _path, key, content_type="application/octet-stream": uploads.append((key, content_type)))
    monkeypatch.setattr(evaluator, "upload_s3_directory", lambda _source, prefix: preview_uploads.append(prefix))

    result = evaluator.run_generation_job(
        {
            "id": "gen-job",
            "job_type": "generation",
            "submission_s3_key": "submissions/sub/submission.zip",
            "dataset_s3_key": "datasets/ds/bundle.zip",
            "task_id": "task-1",
        }
    )

    assert result["artifact_s3_prefix"] == "jobs/gen-job/generation/artifacts"
    assert result["preview_s3_key"] == "jobs/gen-job/generation/preview/index.html"
    assert ("jobs/gen-job/generation/artifacts.zip", "application/zip") in uploads
    assert "jobs/gen-job/generation/preview" in preview_uploads


def test_worker_peer_review_downloads_target_artifact_and_reads_evaluation(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []

    def fake_download_s3(key: str, _target: Path) -> None:
        downloads.append(key)

    def fake_safe_extract(_zip_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        if target.name == "output":
            (target / "source").mkdir()
            (target / "dist").mkdir()
            (target / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
        else:
            (target / "agent.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def fake_copy_task_data(_dataset_key: str, _task_id: str, target: Path) -> Path:
        task = target / "task"
        (task / "data").mkdir(parents=True)
        (task / "task.md").write_text("Task", encoding="utf-8")
        return task

    def fake_run_docker(root: Path, job: dict) -> dict:
        assert job["job_type"] == "peer_review"
        assert (root / "work" / "output" / "dist" / "index.html").exists()
        (root / "reports" / "evaluation").mkdir(parents=True)
        (root / "reports" / "evaluation" / "report.json").write_text(json.dumps({"score": 77}), encoding="utf-8")
        return {
            "started_at": "2026-06-01T00:00:00+00:00",
            "completed_at": "2026-06-01T00:00:01+00:00",
            "run_seconds": 1.0,
            "returncode": 0,
            "log_tail": "",
        }

    monkeypatch.setattr(evaluator, "download_s3", fake_download_s3)
    monkeypatch.setattr(evaluator, "safe_extract_zip", fake_safe_extract)
    monkeypatch.setattr(evaluator, "copy_sdk", lambda _target: None)
    monkeypatch.setattr(evaluator, "copy_task_data", fake_copy_task_data)
    monkeypatch.setattr(evaluator, "run_docker", fake_run_docker)
    monkeypatch.setattr(evaluator, "update_job_runtime_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evaluator, "upload_s3_file", lambda *_args, **_kwargs: None)

    result = evaluator.run_peer_review_job(
        {
            "id": "review-job",
            "job_type": "peer_review",
            "submission_s3_key": "submissions/reviewer/submission.zip",
            "dataset_s3_key": "datasets/ds/bundle.zip",
            "task_id": "task-1",
            "target_artifact_s3_prefix": "jobs/gen-job/generation/artifacts",
        }
    )

    assert downloads == [
        "submissions/reviewer/submission.zip",
        "jobs/gen-job/generation/artifacts.zip",
    ]
    assert result["result"] == {"score": 77}
