from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import connect, decode_json, now_iso, row_to_dict
from .settings import settings

TERMINAL_STATUSES = {"succeeded", "failed", "skipped", "cancelled"}
EVALUATION_JOB_TYPES = {"peer_review", "peer_evaluation", "central_evaluation"}


def open_round(
    name: str,
    starts_at: str | None = None,
    ends_at: str | None = None,
    interval_seconds: int | None = None,
) -> dict[str, Any]:
    now = now_iso()
    interval = interval_seconds or settings.round_interval_seconds
    start = starts_at or now
    end = ends_at or (datetime.fromisoformat(start) + timedelta(seconds=interval)).isoformat()
    round_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            """
            insert into review_rounds (
              id, name, status, starts_at, ends_at, interval_seconds, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (round_id, name, "open", start, end, interval, now, now),
        )
    return get_round(round_id) or {"id": round_id, "name": name, "status": "open", "starts_at": start, "ends_at": end}


def list_rounds(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            select * from review_rounds
            order by coalesce(starts_at, created_at) desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_round(round_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("select * from review_rounds where id = ?", (round_id,)).fetchone()
    return row_to_dict(row)


def get_round_detail(round_id: str) -> dict[str, Any] | None:
    with connect() as db:
        round_row = db.execute("select * from review_rounds where id = ?", (round_id,)).fetchone()
        if round_row is None:
            return None
        participants = db.execute(
            """
            select rp.*, users.email, users.name as user_name, submissions.name as submission_name
            from round_participants rp
            join users on users.id = rp.user_id
            join submissions on submissions.id = rp.submission_id
            where rp.round_id = ?
            order by users.email
            """,
            (round_id,),
        ).fetchall()
        jobs = db.execute(
            """
            select id, submission_id, job_type, generator_submission_id, review_target_job_id,
                   reviewer_user_id, dataset_id, task_id, status, result_json, preview_s3_key,
                   started_at, completed_at, run_seconds, error, created_at, updated_at
            from jobs
            where round_id = ?
            order by created_at
            """,
            (round_id,),
        ).fetchall()
        evaluations = db.execute(
            """
            select *
            from evaluations
            where round_id = ?
            order by created_at
            """,
            (round_id,),
        ).fetchall()
    job_items = []
    for row in jobs:
        item = dict(row)
        item["result"] = decode_json(item.pop("result_json"), None)
        job_items.append(item)
    evaluation_items = []
    for row in evaluations:
        item = dict(row)
        item["result"] = decode_json(item.pop("result_json"), None)
        evaluation_items.append(item)
    return {
        **dict(round_row),
        "participants": [dict(row) for row in participants],
        "jobs": job_items,
        "evaluations": evaluation_items,
        "leaderboard": round_leaderboard(round_id, limit=100),
    }


def close_round(round_id: str) -> dict[str, Any]:
    now = now_iso()
    with connect() as db:
        round_row = db.execute("select * from review_rounds where id = ?", (round_id,)).fetchone()
        if round_row is None:
            raise ValueError(f"Round not found: {round_id}")
        if round_row["status"] not in {"open", "draft"}:
            raise ValueError(f"Round {round_id} is {round_row['status']}, not open")
        starts_at = round_row["starts_at"] or "0001-01-01T00:00:00+00:00"
        ends_at = round_row["ends_at"] or now
        selected = _select_round_participants(db, starts_at, ends_at)
        for participant in selected:
            db.execute(
                """
                insert or replace into round_participants (
                  round_id, user_id, submission_id, selection_reason, selected_at
                )
                values (?, ?, ?, ?, ?)
                """,
                (
                    round_id,
                    participant["user_id"],
                    participant["submission_id"],
                    participant["selection_reason"],
                    now,
                ),
            )
        datasets = db.execute("select id from datasets where visibility = 'public' and task_count > 0").fetchall()
        for participant in selected:
            for dataset in datasets:
                tasks = db.execute("select id from tasks where dataset_id = ?", (dataset["id"],)).fetchall()
                for task in tasks:
                    _insert_generation_job(db, round_id, participant["submission_id"], dataset["id"], task["id"], now)
        db.execute(
            """
            update review_rounds
            set status = ?, generation_started_at = ?, updated_at = ?
            where id = ?
            """,
            ("generation", now, now, round_id),
        )
    return get_round_detail(round_id) or {"id": round_id}


def start_peer_review(round_id: str) -> dict[str, Any]:
    now = now_iso()
    with connect() as db:
        round_row = db.execute("select * from review_rounds where id = ?", (round_id,)).fetchone()
        if round_row is None:
            raise ValueError(f"Round not found: {round_id}")
        if round_row["status"] not in {"generation", "peer_review"}:
            raise ValueError(f"Round {round_id} is {round_row['status']}, not ready for peer review")
        participants = db.execute(
            """
            select rp.user_id, rp.submission_id, users.name as user_name
            from round_participants rp
            join users on users.id = rp.user_id
            where rp.round_id = ?
            """,
            (round_id,),
        ).fetchall()
        artifacts = db.execute(
            """
            select jobs.*, submissions.owner_id as target_owner_id
            from jobs
            join submissions on submissions.id = jobs.submission_id
            where jobs.round_id = ?
              and coalesce(jobs.job_type, 'generation') = 'generation'
              and jobs.status = 'succeeded'
              and jobs.preview_s3_key is not null
            """,
            (round_id,),
        ).fetchall()
        for artifact in artifacts:
            for participant in participants:
                if participant["user_id"] == artifact["target_owner_id"]:
                    continue
                insert_evaluation_job(
                    db,
                    artifact_job=dict(artifact),
                    evaluator_type="peer",
                    evaluator_submission_id=participant["submission_id"],
                    evaluator_user_id=participant["user_id"],
                    evaluator_name=participant["user_name"],
                    job_type="peer_evaluation",
                    now=now,
                )
        db.execute(
            """
            update review_rounds
            set status = ?, peer_review_started_at = coalesce(peer_review_started_at, ?), updated_at = ?
            where id = ?
            """,
            ("peer_review", now, now, round_id),
        )
    return get_round_detail(round_id) or {"id": round_id}


def queue_central_evaluation_for_generation(db, generation_job_id: str, now: str) -> None:
    if not settings.central_judge_submission_id:
        return
    artifact = db.execute(
        """
        select *
        from jobs
        where id = ?
          and coalesce(job_type, 'generation') = 'generation'
          and status = 'succeeded'
          and preview_s3_key is not null
        """,
        (generation_job_id,),
    ).fetchone()
    if artifact is None:
        return
    judge = db.execute(
        """
        select submissions.id, submissions.owner_id, users.name
        from submissions join users on users.id = submissions.owner_id
        where submissions.id = ?
        """,
        (settings.central_judge_submission_id,),
    ).fetchone()
    if judge is None:
        return
    insert_evaluation_job(
        db,
        artifact_job=dict(artifact),
        evaluator_type="central",
        evaluator_submission_id=judge["id"],
        evaluator_user_id=judge["owner_id"],
        evaluator_name=judge["name"],
        job_type="central_evaluation",
        now=now,
    )


def insert_evaluation_job(
    db,
    *,
    artifact_job: dict[str, Any],
    evaluator_type: str,
    evaluator_submission_id: str,
    evaluator_user_id: str,
    evaluator_name: str | None,
    job_type: str,
    now: str,
) -> str | None:
    round_id = artifact_job.get("round_id") or "legacy"
    existing = db.execute(
        """
        select id from evaluations
        where round_id = ?
          and artifact_job_id = ?
          and evaluator_type = ?
          and evaluator_submission_id = ?
        """,
        (round_id, artifact_job["id"], evaluator_type, evaluator_submission_id),
    ).fetchone()
    if existing is not None:
        return None
    job_id = str(uuid.uuid4())
    evaluation_id = str(uuid.uuid4())
    db.execute(
        """
        insert into jobs (
          id, submission_id, job_type, round_id, generator_submission_id,
          review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
          dataset_id, task_id, status, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            evaluator_submission_id,
            job_type,
            artifact_job.get("round_id"),
            artifact_job["submission_id"],
            artifact_job["id"],
            evaluator_user_id,
            now,
            artifact_job["dataset_id"],
            artifact_job["task_id"],
            "queued",
            now,
            now,
        ),
    )
    db.execute(
        """
        insert into evaluations (
          id, round_id, artifact_job_id, evaluator_type, evaluator_user_id,
          evaluator_submission_id, evaluator_name, job_id, status, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evaluation_id,
            round_id,
            artifact_job["id"],
            evaluator_type,
            evaluator_user_id,
            evaluator_submission_id,
            evaluator_name,
            job_id,
            "queued",
            now,
            now,
        ),
    )
    return job_id


def write_self_evaluation_for_generation(db, job: dict[str, Any], result: dict[str, Any], now: str) -> None:
    evaluation = result.get("result")
    if not isinstance(evaluation, dict):
        return
    round_id = job.get("round_id") or "legacy"
    submission = db.execute(
        "select submissions.owner_id, users.name from submissions join users on users.id = submissions.owner_id where submissions.id = ?",
        (job["submission_id"],),
    ).fetchone()
    if submission is None:
        return
    _upsert_evaluation_result(
        db,
        round_id=round_id,
        artifact_job_id=job["id"],
        evaluator_type="self",
        evaluator_user_id=submission["owner_id"],
        evaluator_submission_id=job["submission_id"],
        evaluator_name=submission["name"],
        job_id=job["id"],
        result=result,
        status="succeeded",
        error=None,
        now=now,
    )


def write_evaluation_job_result(db, job: dict[str, Any], result: dict[str, Any], now: str) -> None:
    if (job.get("job_type") or "") not in EVALUATION_JOB_TYPES:
        return
    evaluator_type = {
        "peer_review": "peer",
        "peer_evaluation": "peer",
        "central_evaluation": "central",
    }[job["job_type"]]
    round_id = job.get("round_id") or "legacy"
    submission = db.execute(
        "select submissions.owner_id, users.name from submissions join users on users.id = submissions.owner_id where submissions.id = ?",
        (job["submission_id"],),
    ).fetchone()
    _upsert_evaluation_result(
        db,
        round_id=round_id,
        artifact_job_id=job["review_target_job_id"],
        evaluator_type=evaluator_type,
        evaluator_user_id=job.get("reviewer_user_id") or (submission["owner_id"] if submission else None),
        evaluator_submission_id=job["submission_id"],
        evaluator_name=submission["name"] if submission else None,
        job_id=job["id"],
        result=result,
        status="succeeded",
        error=None,
        now=now,
    )


def write_evaluation_job_failure(db, job: dict[str, Any], error: str, now: str) -> None:
    if (job.get("job_type") or "") not in EVALUATION_JOB_TYPES:
        return
    evaluator_type = {
        "peer_review": "peer",
        "peer_evaluation": "peer",
        "central_evaluation": "central",
    }[job["job_type"]]
    round_id = job.get("round_id") or "legacy"
    _upsert_evaluation_result(
        db,
        round_id=round_id,
        artifact_job_id=job["review_target_job_id"],
        evaluator_type=evaluator_type,
        evaluator_user_id=job.get("reviewer_user_id"),
        evaluator_submission_id=job["submission_id"],
        evaluator_name=None,
        job_id=job["id"],
        result={},
        status="failed",
        error=error,
        now=now,
    )


def complete_round_if_ready(round_id: str) -> dict[str, Any] | None:
    now = now_iso()
    with connect() as db:
        round_row = db.execute("select * from review_rounds where id = ?", (round_id,)).fetchone()
        if round_row is None:
            return None
        pending = db.execute(
            """
            select count(*) as count
            from evaluations
            where round_id = ? and status not in ('succeeded', 'failed', 'skipped', 'cancelled')
            """,
            (round_id,),
        ).fetchone()["count"]
        if pending:
            return dict(round_row)
        scores = round_leaderboard(round_id, limit=100000)
        for score in scores:
            db.execute("update submissions set score = ?, status = ? where id = ?", (score["round_score"], "succeeded", score["submission_id"]))
        db.execute(
            """
            update review_rounds
            set status = ?, completed_at = coalesce(completed_at, ?), updated_at = ?
            where id = ?
            """,
            ("complete", now, now, round_id),
        )
    return get_round(round_id)


def round_leaderboard(round_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            with artifact_scores as (
              select j.submission_id,
                     e.artifact_job_id,
                     coalesce(
                       avg(case when e.evaluator_type='peer' and e.status='succeeded' then e.score end),
                       avg(case when e.evaluator_type='central' and e.status='succeeded' then e.score end),
                       avg(case when e.evaluator_type='self' and e.status='succeeded' then e.score end)
                     ) as artifact_score
              from evaluations e
              join jobs j on j.id = e.artifact_job_id
              where e.round_id = ?
              group by j.submission_id, e.artifact_job_id
            )
            select submissions.id as submission_id,
                   submissions.name as submission_name,
                   users.id as user_id,
                   users.name as owner_name,
                   avg(artifact_scores.artifact_score) as round_score
            from artifact_scores
            join submissions on submissions.id = artifact_scores.submission_id
            join users on users.id = submissions.owner_id
            where artifact_scores.artifact_score is not null
            group by submissions.id, submissions.name, users.id, users.name
            order by round_score desc
            limit ?
            """,
            (round_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def maybe_open_next_round() -> dict[str, Any] | None:
    if not settings.rounds_enabled:
        return None
    now = datetime.now(UTC)
    with connect() as db:
        open_existing = db.execute("select id from review_rounds where status in ('open', 'generation', 'peer_review') limit 1").fetchone()
        if open_existing:
            return None
        last = db.execute("select starts_at from review_rounds order by starts_at desc limit 1").fetchone()
    if last and last["starts_at"]:
        last_start = datetime.fromisoformat(last["starts_at"])
        if (now - last_start).total_seconds() < settings.round_interval_seconds:
            return None
    return open_round(
        f"Round {now.strftime('%Y-%m-%d %H:%M UTC')}",
        starts_at=now.isoformat(),
        ends_at=(now + timedelta(seconds=settings.round_interval_seconds)).isoformat(),
        interval_seconds=settings.round_interval_seconds,
    )


def advance_due_rounds() -> None:
    now = datetime.now(UTC)
    with connect() as db:
        rounds = [dict(row) for row in db.execute("select * from review_rounds where status in ('open', 'generation', 'peer_review')").fetchall()]
    for round_row in rounds:
        if round_row["status"] == "open" and round_row.get("ends_at") and datetime.fromisoformat(round_row["ends_at"]) <= now:
            close_round(round_row["id"])
        elif round_row["status"] == "generation" and settings.auto_start_peer_review and _round_generation_done(round_row["id"]):
            start_peer_review(round_row["id"])
        elif round_row["status"] == "peer_review":
            complete_round_if_ready(round_row["id"])


def _select_round_participants(db, starts_at: str, ends_at: str) -> list[dict[str, str]]:
    rows = db.execute(
        """
        select *
        from (
          select submissions.id as submission_id,
                 submissions.owner_id as user_id,
                 submissions.finalized_at,
                 row_number() over (
                   partition by submissions.owner_id
                   order by submissions.finalized_at desc, submissions.created_at desc
                 ) as rank
          from submissions
          where submissions.finalized_at is not null
            and submissions.finalized_at <= ?
        )
        where rank = 1
        order by user_id
        """,
        (ends_at,),
    ).fetchall()
    return [
        {
            "user_id": row["user_id"],
            "submission_id": row["submission_id"],
            "selection_reason": "interval_latest" if starts_at <= row["finalized_at"] < ends_at else "carried_forward",
        }
        for row in rows
    ]


def _insert_generation_job(db, round_id: str, submission_id: str, dataset_id: str, task_id: str, now: str) -> None:
    existing = db.execute(
        """
        select id from jobs
        where round_id = ?
          and coalesce(job_type, 'generation') = 'generation'
          and submission_id = ?
          and dataset_id = ?
          and task_id = ?
        """,
        (round_id, submission_id, dataset_id, task_id),
    ).fetchone()
    if existing:
        return
    db.execute(
        """
        insert into jobs (
          id, submission_id, job_type, round_id, generator_submission_id,
          dataset_id, task_id, status, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), submission_id, "generation", round_id, submission_id, dataset_id, task_id, "queued", now, now),
    )


def _upsert_evaluation_result(
    db,
    *,
    round_id: str,
    artifact_job_id: str,
    evaluator_type: str,
    evaluator_user_id: str | None,
    evaluator_submission_id: str,
    evaluator_name: str | None,
    job_id: str,
    result: dict[str, Any],
    status: str,
    error: str | None,
    now: str,
) -> None:
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    score = payload.get("score") if isinstance(payload, dict) else None
    max_score = payload.get("max_score") if isinstance(payload, dict) else None
    existing = db.execute(
        """
        select id from evaluations
        where round_id = ?
          and artifact_job_id = ?
          and evaluator_type = ?
          and evaluator_submission_id = ?
        """,
        (round_id, artifact_job_id, evaluator_type, evaluator_submission_id),
    ).fetchone()
    if existing:
        db.execute(
            """
            update evaluations
            set evaluator_user_id = ?, evaluator_name = ?, job_id = ?, status = ?,
                score = ?, max_score = ?, result_json = ?,
                evaluation_report_s3_key = ?, evaluation_trajectory_s3_key = ?,
                run_seconds = ?, error = ?, completed_at = ?, updated_at = ?
            where id = ?
            """,
            (
                evaluator_user_id,
                evaluator_name,
                job_id,
                status,
                score,
                max_score,
                json.dumps(payload) if isinstance(payload, dict) else None,
                result.get("evaluation_report_s3_key"),
                result.get("evaluation_trajectory_s3_key"),
                result.get("run_seconds"),
                error,
                result.get("completed_at") or now if status in TERMINAL_STATUSES else None,
                now,
                existing["id"],
            ),
        )
        return
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
            str(uuid.uuid4()),
            round_id,
            artifact_job_id,
            evaluator_type,
            evaluator_user_id,
            evaluator_submission_id,
            evaluator_name,
            job_id,
            status,
            score,
            max_score,
            json.dumps(payload) if isinstance(payload, dict) else None,
            result.get("evaluation_report_s3_key"),
            result.get("evaluation_trajectory_s3_key"),
            result.get("run_seconds"),
            error,
            now,
            result.get("completed_at") or now if status in TERMINAL_STATUSES else None,
            now,
        ),
    )


def _round_generation_done(round_id: str) -> bool:
    with connect() as db:
        pending = db.execute(
            """
            select count(*) as count
            from jobs
            where round_id = ?
              and coalesce(job_type, 'generation') = 'generation'
              and status not in ('succeeded', 'failed', 'skipped', 'cancelled')
            """,
            (round_id,),
        ).fetchone()["count"]
    return int(pending or 0) == 0
