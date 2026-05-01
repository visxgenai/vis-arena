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
              created_at text not null
            );
            create table if not exists jobs (
              id text primary key,
              submission_id text not null,
              dataset_id text,
              task_id text,
              status text not null,
              result_json text,
              artifact_s3_prefix text,
              error text,
              created_at text not null,
              updated_at text not null
            );
            """
        )
        _add_column(db, "datasets", "s3_key text")
        _add_column(db, "submissions", "s3_key text")
        _add_column(db, "submissions", "storage_path text not null default ''")
        _add_column(db, "jobs", "task_id text")
        _add_column(db, "jobs", "artifact_s3_prefix text")
        _add_column(db, "jobs", "error text")
        _add_column(db, "jobs", "arena_round integer")
        _add_column(db, "jobs", "arena_evaluators text")


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
