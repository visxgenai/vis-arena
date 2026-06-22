from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .settings import settings


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_db() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            create table if not exists users (
              id text primary key,
              email text unique not null,
              password_hash text not null,
              name text,
              created_at text not null
            );
            create table if not exists datasets (
              id text primary key,
              owner_id text not null,
              name text not null,
              visibility text not null,
              task_count integer not null default 0,
              s3_key text,
              storage_path text not null default '',
              created_at text not null
            );
            create table if not exists tasks (
              id text primary key,
              dataset_id text not null,
              title text not null,
              version integer not null,
              metadata_json text not null,
              task_path text not null
            );
            create table if not exists submissions (
              id text primary key,
              owner_id text not null,
              name text not null,
              status text not null,
              score real,
              s3_key text,
              storage_path text not null default '',
              finalized_at text,
              reviewer_eligible_at text,
              created_at text not null
            );
            create table if not exists jobs (
              id text primary key,
              submission_id text not null,
              job_type text not null default 'generation',
              round_id text,
              generator_submission_id text,
              review_target_job_id text,
              reviewer_user_id text,
              reviewer_cutoff_at text,
              dataset_id text,
              task_id text,
              status text not null,
              result_json text,
              artifact_s3_prefix text,
              preview_s3_key text,
              generation_s3_prefix text,
              evaluation_s3_prefix text,
              agent_info_s3_key text,
              generation_trajectory_s3_key text,
              evaluation_trajectory_s3_key text,
              generation_agent_trajectory_s3_key text,
              evaluation_agent_trajectory_s3_key text,
              evaluation_report_s3_key text,
              error text,
              started_at text,
              completed_at text,
              run_seconds real,
              generation_run_seconds real,
              self_evaluation_run_seconds real,
              executor text,
              external_job_id text,
              dispatched_at text,
              last_heartbeat_at text,
              executor_error text,
              created_at text not null,
              updated_at text not null
            );
            create table if not exists llm_usage (
              id text primary key,
              job_id text not null,
              submission_id text not null,
              user_id text not null,
              provider text not null,
              model_id text not null,
              purpose text not null,
              input_tokens integer not null,
              output_tokens integer not null,
              total_tokens integer not null,
              estimated_cost_usd real,
              latency_ms integer not null,
              created_at text not null
            );
            create table if not exists review_rounds (
              id text primary key,
              name text not null,
              status text not null,
              starts_at text,
              ends_at text,
              generation_started_at text,
              peer_review_started_at text,
              completed_at text,
              interval_seconds integer,
              created_at text not null,
              updated_at text not null
            );
            create table if not exists round_participants (
              round_id text not null,
              user_id text not null,
              submission_id text not null,
              selection_reason text not null,
              selected_at text not null,
              primary key (round_id, user_id)
            );
            create table if not exists evaluations (
              id text primary key,
              round_id text not null,
              artifact_job_id text not null,
              evaluator_type text not null,
              evaluator_user_id text,
              evaluator_submission_id text not null,
              evaluator_name text,
              job_id text,
              status text not null,
              score real,
              max_score real,
              result_json text,
              evaluation_report_s3_key text,
              evaluation_trajectory_s3_key text,
              run_seconds real,
              source_evaluation_id text,
              carried_from_round_id text,
              is_carried_forward integer not null default 0,
              error text,
              created_at text not null,
              completed_at text,
              updated_at text not null
            );
            """
        )
        _add_column(db, "datasets", "s3_key text")
        _add_column(db, "submissions", "s3_key text")
        _add_column(db, "submissions", "finalized_at text")
        _add_column(db, "submissions", "reviewer_eligible_at text")
        _add_column(db, "jobs", "job_type text not null default 'generation'")
        _add_column(db, "jobs", "round_id text")
        _add_column(db, "jobs", "generator_submission_id text")
        _add_column(db, "jobs", "review_target_job_id text")
        _add_column(db, "jobs", "reviewer_user_id text")
        _add_column(db, "jobs", "reviewer_cutoff_at text")
        _add_column(db, "jobs", "task_id text")
        _add_column(db, "jobs", "artifact_s3_prefix text")
        _add_column(db, "jobs", "preview_s3_key text")
        _add_column(db, "jobs", "generation_s3_prefix text")
        _add_column(db, "jobs", "evaluation_s3_prefix text")
        _add_column(db, "jobs", "agent_info_s3_key text")
        _add_column(db, "jobs", "generation_trajectory_s3_key text")
        _add_column(db, "jobs", "evaluation_trajectory_s3_key text")
        _add_column(db, "jobs", "generation_agent_trajectory_s3_key text")
        _add_column(db, "jobs", "evaluation_agent_trajectory_s3_key text")
        _add_column(db, "jobs", "evaluation_report_s3_key text")
        _add_column(db, "jobs", "error text")
        _add_column(db, "jobs", "started_at text")
        _add_column(db, "jobs", "completed_at text")
        _add_column(db, "jobs", "run_seconds real")
        _add_column(db, "jobs", "generation_run_seconds real")
        _add_column(db, "jobs", "self_evaluation_run_seconds real")
        _add_column(db, "jobs", "executor text")
        _add_column(db, "jobs", "external_job_id text")
        _add_column(db, "jobs", "dispatched_at text")
        _add_column(db, "jobs", "last_heartbeat_at text")
        _add_column(db, "jobs", "executor_error text")
        _add_column(db, "evaluations", "source_evaluation_id text")
        _add_column(db, "evaluations", "carried_from_round_id text")
        _add_column(db, "evaluations", "is_carried_forward integer not null default 0")
        db.executescript(
            """
            create index if not exists idx_jobs_round_type on jobs(round_id, job_type);
            create index if not exists idx_jobs_executor_status on jobs(executor, status);
            create index if not exists idx_evaluations_round_artifact on evaluations(round_id, artifact_job_id);
            create index if not exists idx_evaluations_evaluator_sub on evaluations(evaluator_submission_id);
            create index if not exists idx_evaluations_reuse_lookup on evaluations(
              artifact_job_id, evaluator_type, evaluator_submission_id, status
            );
            create index if not exists idx_round_participants_user on round_participants(user_id);
            create unique index if not exists idx_evaluations_unique on evaluations(
              round_id, artifact_job_id, evaluator_type, evaluator_submission_id
            );
            """
        )


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(settings.database_path)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def decode_json(value: str | None, default):
    if not value:
        return default
    return json.loads(value)


def _add_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    rows = db.execute(f"pragma table_info({table})").fetchall()
    if column not in {row["name"] for row in rows}:
        db.execute(f"alter table {table} add column {definition}")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
