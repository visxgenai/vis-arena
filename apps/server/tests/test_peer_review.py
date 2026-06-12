from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from vis_arena_server import evaluator, rounds, storage
from vis_arena_server.db import connect, init_db, now_iso
from vis_arena_server.settings import settings


@pytest.fixture(autouse=True)
def _clean_db() -> None:
    settings.legacy_peer_review_enabled = False
    settings.rounds_enabled = False
    settings.auto_start_peer_review = False
    init_db()
    with connect() as db:
        for table in ("llm_usage", "evaluations", "round_participants", "jobs", "review_rounds", "tasks", "submissions", "datasets", "users"):
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
            "generation_run_seconds": 0.25,
            "self_evaluation_run_seconds": 0.75,
        },
    )


def test_generation_completion_stores_phase_and_self_evaluation_runtime() -> None:
    owner_id = _insert_user()
    dataset_id, (task_id,) = _insert_dataset()
    submission_id = _insert_submission(owner_id, status="running")
    job_id = _insert_generation_job(submission_id, dataset_id, task_id, status="running")

    _complete_generation(job_id)

    with connect() as db:
        job = db.execute("select run_seconds, generation_run_seconds, self_evaluation_run_seconds from jobs where id = ?", (job_id,)).fetchone()
        evaluation = db.execute("select evaluator_type, run_seconds from evaluations where artifact_job_id = ? and evaluator_type = 'self'", (job_id,)).fetchone()
        peer_count = db.execute("select count(*) as count from jobs where job_type = 'peer_review'").fetchone()["count"]

    assert dict(job) == {
        "run_seconds": 1.0,
        "generation_run_seconds": 0.25,
        "self_evaluation_run_seconds": 0.75,
    }
    assert dict(evaluation) == {"evaluator_type": "self", "run_seconds": 0.75}
    assert peer_count == 0


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


def test_public_leaderboard_groups_participants_and_round_history(client: TestClient) -> None:
    owner_id = _insert_user("owner-a")
    reviewer_id = _insert_user("owner-b")
    dataset_id, (task_id,) = _insert_dataset()
    old_submission = _insert_submission(
        owner_id,
        status="succeeded",
        finalized_at="2026-06-01T00:00:00+00:00",
        created_at="2026-06-01T00:00:00+00:00",
    )
    latest_submission = _insert_submission(
        owner_id,
        status="succeeded",
        finalized_at="2026-06-02T00:00:00+00:00",
        created_at="2026-06-02T00:00:00+00:00",
    )
    reviewer_submission = _insert_submission(
        reviewer_id,
        status="succeeded",
        finalized_at="2026-06-02T00:01:00+00:00",
        created_at="2026-06-02T00:01:00+00:00",
    )
    old_job = _insert_generation_job(old_submission, dataset_id, task_id, status="succeeded")
    latest_job = _insert_generation_job(latest_submission, dataset_id, task_id, status="succeeded")
    round_id = _id("round")
    now = "2026-06-02T01:00:00+00:00"
    with connect() as db:
        db.execute("update submissions set score = ? where id = ?", (68, old_submission))
        db.execute("update submissions set score = ? where id = ?", (82, latest_submission))
        db.execute("update submissions set score = ? where id = ?", (70, reviewer_submission))
        db.execute(
            """
            update jobs
            set result_json = ?, preview_s3_key = ?, completed_at = ?, run_seconds = ?
            where id in (?, ?)
            """,
            (
                json.dumps({"score": 82, "max_score": 100, "summary": "ok"}),
                f"jobs/{latest_job}/generation/preview/index.html",
                now,
                2.5,
                old_job,
                latest_job,
            ),
        )
        db.execute(
            """
            insert into review_rounds (
              id, name, status, starts_at, ends_at, generation_started_at,
              peer_review_started_at, completed_at, interval_seconds, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                "Round 1",
                "complete",
                "2026-06-02T00:00:00+00:00",
                "2026-06-02T01:00:00+00:00",
                now,
                now,
                now,
                3600,
                now,
                now,
            ),
        )
        for user_id, submission_id, reason in (
            (owner_id, latest_submission, "interval_latest"),
            (reviewer_id, reviewer_submission, "interval_latest"),
        ):
            db.execute(
                "insert into round_participants (round_id, user_id, submission_id, selection_reason, selected_at) values (?, ?, ?, ?, ?)",
                (round_id, user_id, submission_id, reason, now),
            )
        db.execute(
            """
            insert into evaluations (
              id, round_id, artifact_job_id, evaluator_type, evaluator_user_id,
              evaluator_submission_id, evaluator_name, job_id, status, score,
              max_score, result_json, evaluation_report_s3_key,
              evaluation_trajectory_s3_key, run_seconds, error, created_at,
              completed_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("eval"),
                round_id,
                latest_job,
                "peer",
                reviewer_id,
                reviewer_submission,
                "reviewer",
                _id("review-job"),
                "succeeded",
                82,
                100,
                json.dumps({"score": 82, "max_score": 100}),
                f"jobs/{latest_job}/evaluation/report.json",
                f"jobs/{latest_job}/evaluation/trajectory.jsonl",
                3.0,
                None,
                now,
                now,
                now,
            ),
        )

    res = client.get("/v1/leaderboard")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"]
    assert body["participants"][0]["user_id"] == owner_id
    assert body["participants"][0]["best_score"] == 82
    assert body["participants"][0]["best_submission"]["id"] == latest_submission
    assert body["participants"][0]["best_submission"]["jobs"][0]["evaluations"][0]["score"] == 82
    assert body["rounds"][0]["participants"][0]["is_new_submission"] is True
    assert "owner_email" not in body["participants"][0]


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
    settings.legacy_peer_review_enabled = True
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
    settings.legacy_peer_review_enabled = True
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
    settings.legacy_peer_review_enabled = True
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
    settings.legacy_peer_review_enabled = True
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
    phases: list[str] = []

    def fake_safe_extract(_zip_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "agent.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def fake_copy_task_data(_dataset_key: str, _task_id: str, target: Path) -> Path:
        task = target / "task"
        (task / "data").mkdir(parents=True)
        (task / "task.md").write_text("Task", encoding="utf-8")
        return task

    def fake_run_docker(root: Path, job: dict, *, phase: str, artifact_url: str | None = None) -> dict:
        phases.append(phase)
        assert (job.get("job_type") or "generation") == "generation"
        if phase == "generation":
            assert artifact_url is None
            (root / "work" / "generate" / "source").mkdir(parents=True)
            (root / "work" / "generate" / "dist").mkdir(parents=True)
            (root / "work" / "generate" / "source" / "main.js").write_text("console.log('ok')", encoding="utf-8")
            (root / "work" / "generate" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
            (root / "reports" / "generation").mkdir(parents=True)
            (root / "reports" / "generation" / "agent-info.json").write_text("{}", encoding="utf-8")
            (root / "reports" / "generation" / "runtime.log").write_text("generation", encoding="utf-8")
        else:
            assert phase == "evaluation"
            assert artifact_url == "http://host.docker.internal:8000/v1/jobs/gen-job/preview"
            (root / "work" / "evaluate" / "evaluation.json").write_text(json.dumps({"score": 88}), encoding="utf-8")
            (root / "reports" / "evaluation").mkdir(parents=True)
            (root / "reports" / "evaluation" / "runtime.log").write_text("evaluation", encoding="utf-8")
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
    assert result["result"] == {"score": 88}
    assert phases == ["generation", "evaluation"]
    assert ("jobs/gen-job/generation/artifacts.zip", "application/zip") in uploads
    assert "jobs/gen-job/generation/preview" in preview_uploads


def test_worker_peer_review_uses_target_preview_url_and_reads_evaluation(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []

    def fake_download_s3(key: str, _target: Path) -> None:
        downloads.append(key)

    def fake_safe_extract(_zip_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "agent.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def fake_copy_task_data(_dataset_key: str, _task_id: str, target: Path) -> Path:
        task = target / "task"
        (task / "data").mkdir(parents=True)
        (task / "task.md").write_text("Task", encoding="utf-8")
        return task

    def fake_run_docker(root: Path, job: dict, *, phase: str, artifact_url: str | None = None) -> dict:
        assert job["job_type"] == "peer_review"
        assert phase == "evaluation"
        assert artifact_url == "http://host.docker.internal:8000/v1/jobs/gen-job/preview"
        assert (root / "work" / "evaluate" / "task.md").exists()
        assert not (root / "work" / "evaluate" / "dist").exists()
        (root / "reports" / "evaluation").mkdir(parents=True)
        (root / "work" / "evaluate" / "evaluation.json").write_text(json.dumps({"score": 77}), encoding="utf-8")
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
            "review_target_job_id": "gen-job",
            "target_preview_s3_key": "jobs/gen-job/generation/preview/index.html",
        }
    )

    assert downloads == ["submissions/reviewer/submission.zip"]
    assert result["result"] == {"score": 77}


def test_round_close_snapshots_latest_submission_per_user_and_queues_generation_jobs() -> None:
    start = "2026-06-01T00:00:00+00:00"
    end = "2026-06-01T01:00:00+00:00"
    first_owner = _insert_user()
    second_owner = _insert_user()
    late_owner = _insert_user()
    _dataset_id, task_ids = _insert_dataset(task_count=2)
    old_first_submission = _insert_submission(first_owner, finalized_at="2026-05-31T23:00:00+00:00")
    latest_first_submission = _insert_submission(first_owner, finalized_at="2026-06-01T00:30:00+00:00")
    carried_second_submission = _insert_submission(second_owner, finalized_at="2026-05-31T22:00:00+00:00")
    _late_submission = _insert_submission(late_owner, finalized_at="2026-06-01T01:01:00+00:00")
    round_id = rounds.open_round("Smoke Round", starts_at=start, ends_at=end)["id"]

    detail = rounds.close_round(round_id)

    assert detail["status"] == "generation"
    participants = {item["user_id"]: item for item in detail["participants"]}
    assert participants[first_owner]["submission_id"] == latest_first_submission
    assert participants[first_owner]["selection_reason"] == "interval_latest"
    assert participants[second_owner]["submission_id"] == carried_second_submission
    assert participants[second_owner]["selection_reason"] == "carried_forward"
    assert late_owner not in participants
    assert old_first_submission not in {item["submission_id"] for item in participants.values()}
    with connect() as db:
        jobs = db.execute(
            """
            select round_id, submission_id, job_type, task_id, status
            from jobs
            where round_id = ?
            order by submission_id, task_id
            """,
            (round_id,),
        ).fetchall()

    assert len(jobs) == 2 * len(task_ids)
    assert {row["submission_id"] for row in jobs} == {latest_first_submission, carried_second_submission}
    assert {row["job_type"] for row in jobs} == {"generation"}
    assert {row["status"] for row in jobs} == {"queued"}


def test_round_admin_api_requires_admin_and_opens_round(client: TestClient, monkeypatch) -> None:
    admin_email = f"{_id('admin')}@example.com"
    admin_res = client.post("/v1/auth/register", json={"email": admin_email, "password": "password123", "name": "Admin"})
    assert admin_res.status_code == 200, admin_res.text
    admin_auth = {"Authorization": f"Bearer {admin_res.json()['access_token']}"}
    _user_id, non_admin_auth = _register(client)
    monkeypatch.setenv("VIS_ARENA_ADMIN_EMAILS", admin_email)

    forbidden = client.post("/v1/peer-reviews/rounds", headers=non_admin_auth, json={"name": "Denied Round"})
    opened = client.post(
        "/v1/peer-reviews/rounds",
        headers=admin_auth,
        json={
            "name": "API Round",
            "starts_at": "2026-06-01T00:00:00+00:00",
            "ends_at": "2026-06-01T01:00:00+00:00",
        },
    )
    listed = client.get("/v1/peer-reviews/rounds", headers=admin_auth)

    assert forbidden.status_code == 403
    assert opened.status_code == 200, opened.text
    assert opened.json()["name"] == "API Round"
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [opened.json()["id"]]


def test_round_peer_review_queues_cross_user_evaluations_once_and_rolls_up_scores() -> None:
    dataset_id, (task_id,) = _insert_dataset(task_count=1)
    first_owner = _insert_user()
    second_owner = _insert_user()
    first_submission = _insert_submission(first_owner, finalized_at="2026-06-01T00:10:00+00:00")
    second_submission = _insert_submission(second_owner, finalized_at="2026-06-01T00:20:00+00:00")
    round_id = rounds.open_round(
        "Peer Round",
        starts_at="2026-06-01T00:00:00+00:00",
        ends_at="2026-06-01T01:00:00+00:00",
    )["id"]
    rounds.close_round(round_id)
    with connect() as db:
        generation_jobs = db.execute(
            "select id, submission_id from jobs where round_id = ? and job_type = 'generation'",
            (round_id,),
        ).fetchall()
        assert len(generation_jobs) == 2
        for job in generation_jobs:
            db.execute(
                """
                update jobs
                set status = 'succeeded',
                    preview_s3_key = ?,
                    artifact_s3_prefix = ?,
                    completed_at = ?,
                    updated_at = ?
                where id = ?
                """,
                (
                    f"jobs/{job['id']}/generation/preview/index.html",
                    f"jobs/{job['id']}/generation/artifacts",
                    now_iso(),
                    now_iso(),
                    job["id"],
                ),
            )

    first_detail = rounds.start_peer_review(round_id)
    second_detail = rounds.start_peer_review(round_id)

    assert first_detail["status"] == "peer_review"
    assert len(first_detail["evaluations"]) == 2
    assert len(second_detail["evaluations"]) == 2
    with connect() as db:
        review_jobs = db.execute(
            """
            select id, submission_id, generator_submission_id, review_target_job_id,
                   reviewer_user_id, job_type, status
            from jobs
            where round_id = ? and job_type = 'peer_evaluation'
            order by submission_id
            """,
            (round_id,),
        ).fetchall()
        evaluations = db.execute("select * from evaluations where round_id = ? order by evaluator_submission_id", (round_id,)).fetchall()

    assert len(review_jobs) == 2
    assert len(evaluations) == 2
    assert {row["job_type"] for row in review_jobs} == {"peer_evaluation"}
    assert {row["status"] for row in review_jobs} == {"queued"}
    for job in review_jobs:
        assert job["submission_id"] != job["generator_submission_id"]
        if job["submission_id"] == first_submission:
            assert job["reviewer_user_id"] == first_owner
            assert job["generator_submission_id"] == second_submission
        else:
            assert job["submission_id"] == second_submission
            assert job["reviewer_user_id"] == second_owner
            assert job["generator_submission_id"] == first_submission

    for job in review_jobs:
        score = 90 if job["generator_submission_id"] == first_submission else 70
        evaluator.complete_job(
            job["id"],
            {
                "result": {"score": score, "max_score": 100},
                "evaluation_report_s3_key": f"jobs/{job['id']}/evaluation/report.json",
                "evaluation_trajectory_s3_key": f"jobs/{job['id']}/evaluation/trajectory.jsonl",
                "started_at": "2026-06-01T01:00:00+00:00",
                "completed_at": "2026-06-01T01:00:02+00:00",
                "run_seconds": 2.0,
            },
        )

    leaderboard = rounds.round_leaderboard(round_id)
    with connect() as db:
        completed_round = db.execute("select status from review_rounds where id = ?", (round_id,)).fetchone()
        first_row = db.execute("select status, score from submissions where id = ?", (first_submission,)).fetchone()
        second_row = db.execute("select status, score from submissions where id = ?", (second_submission,)).fetchone()
        stored_evals = db.execute("select status, score, run_seconds, evaluation_report_s3_key from evaluations where round_id = ?", (round_id,)).fetchall()

    assert completed_round["status"] == "complete"
    assert {item["submission_id"]: item["round_score"] for item in leaderboard} == {
        first_submission: 90.0,
        second_submission: 70.0,
    }
    assert dict(first_row) == {"status": "succeeded", "score": 90.0}
    assert dict(second_row) == {"status": "succeeded", "score": 70.0}
    assert {row["status"] for row in stored_evals} == {"succeeded"}
    assert {row["run_seconds"] for row in stored_evals} == {2.0}
    assert all(row["evaluation_report_s3_key"] for row in stored_evals)
