"""Central judge queuing: gated on settings, filtered by task."""
from __future__ import annotations

import uuid

import pytest

from vis_arena_server.db import connect, init_db, now_iso
from vis_arena_server.rounds import queue_central_evaluation_for_generation
from vis_arena_server.settings import settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch) -> None:
    init_db()
    with connect() as db:
        for table in ("llm_usage", "evaluations", "round_participants", "jobs", "review_rounds", "submissions", "users"):
            db.execute(f"delete from {table}")
    monkeypatch.setattr(settings, "central_judge_submission_id", None)
    monkeypatch.setattr(settings, "central_judge_tasks", "")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _seed_artifact(task_id: str) -> tuple[str, str]:
    """One participant with a succeeded generation artifact; returns (gen_job_id, judge_submission_id)."""
    now = now_iso()
    with connect() as db:
        for role in ("player", "judge"):
            db.execute(
                "insert into users (id, email, password_hash, name, created_at) values (?, ?, ?, ?, ?)",
                (f"{role}-u", f"{role}@example.com", "hash", role, now),
            )
        for role in ("player", "judge"):
            db.execute(
                "insert into submissions (id, owner_id, name, status, score, s3_key, finalized_at, created_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{role}-sub", f"{role}-u", role, "succeeded", None, f"submissions/{role}-sub/submission.zip", now, now),
            )
        job_id = _id("gen")
        db.execute(
            "insert into jobs (id, submission_id, job_type, generator_submission_id, dataset_id, task_id, "
            "status, preview_s3_key, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, "player-sub", "generation", "player-sub", "ds", task_id, "succeeded", "preview/index.html", now, now),
        )
    return job_id, "judge-sub"


def _central_jobs() -> int:
    with connect() as db:
        return db.execute("select count(*) from jobs where job_type='central_evaluation'").fetchone()[0]


def test_disabled_without_submission_id():
    job_id, _ = _seed_artifact("sailor-shift-career")
    with connect() as db:
        queue_central_evaluation_for_generation(db, job_id, now_iso())
    assert _central_jobs() == 0


def test_queues_when_enabled_no_filter():
    job_id, judge = _seed_artifact("sailor-shift-career")
    settings.central_judge_submission_id = judge
    with connect() as db:
        queue_central_evaluation_for_generation(db, job_id, now_iso())
    assert _central_jobs() == 1
    with connect() as db:
        row = db.execute("select evaluator_type, evaluator_submission_id from evaluations").fetchone()
    assert row["evaluator_type"] == "central"
    assert row["evaluator_submission_id"] == judge


def test_task_filter_skips_other_tasks():
    job_id, judge = _seed_artifact("visualization-publications-v1")
    settings.central_judge_submission_id = judge
    settings.central_judge_tasks = "sailor-shift-career"
    with connect() as db:
        queue_central_evaluation_for_generation(db, job_id, now_iso())
    assert _central_jobs() == 0


def test_task_filter_matches():
    job_id, judge = _seed_artifact("sailor-shift-career")
    settings.central_judge_submission_id = judge
    settings.central_judge_tasks = "sailor-shift-career, some-other-task"
    with connect() as db:
        queue_central_evaluation_for_generation(db, job_id, now_iso())
    assert _central_jobs() == 1
