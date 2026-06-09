from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .auth import create_token
from .db import connect, now_iso, row_to_dict
from .rounds import (
    EVALUATION_JOB_TYPES,
    advance_due_rounds,
    complete_round_if_ready,
    maybe_open_next_round,
    queue_central_evaluation_for_generation,
    write_evaluation_job_failure,
    write_evaluation_job_result,
    write_self_evaluation_for_generation,
)
from .settings import settings
from .storage import copy_task_data, download_s3, make_zip, safe_extract_zip, upload_s3_directory, upload_s3_file


def run() -> None:
    stop_event = threading.Event()
    if settings.rounds_enabled:
        scheduler = threading.Thread(target=round_trigger_loop, args=(stop_event,), daemon=True)
        scheduler.start()
    try:
        while True:
            job = claim_job()
            if job is None:
                time.sleep(3)
                continue
            try:
                result = run_job(job)
                complete_job(job["id"], result)
            except Exception as exc:
                fail_job(job["id"], exc)
    finally:
        stop_event.set()


def round_trigger_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            maybe_open_next_round()
            advance_due_rounds()
        except Exception:
            # Keep the worker alive; job failures are recorded per job, but the
            # scheduler should retry transient DB/config issues on the next tick.
            pass
        stop_event.wait(60)


def claim_job() -> dict[str, Any] | None:
    refresh_waiting_reviews()
    with connect() as db:
        row = db.execute(
            """
            select jobs.*,
                   submissions.owner_id,
                   submissions.s3_key as submission_s3_key,
                   datasets.s3_key as dataset_s3_key,
                   target.artifact_s3_prefix as target_artifact_s3_prefix,
                   target.preview_s3_key as target_preview_s3_key
            from jobs
            join submissions on submissions.id = jobs.submission_id
            join datasets on datasets.id = jobs.dataset_id
            left join jobs target on target.id = jobs.review_target_job_id
            where jobs.status = 'queued'
            order by case coalesce(jobs.job_type, 'generation') when 'generation' then 0 else 1 end,
                     jobs.created_at
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        now = now_iso()
        db.execute("update jobs set status = ?, updated_at = ? where id = ?", ("running", now, row["id"]))
        db.execute("update submissions set status = ? where id = ?", ("running", row["generator_submission_id"] or row["submission_id"]))
        return dict(row)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    if (job.get("job_type") or "generation") in EVALUATION_JOB_TYPES:
        return run_peer_review_job(job)
    return run_generation_job(job)


def run_generation_job(job: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        submission_zip = root / "submission.zip"
        submission_dir = root / "submission"
        work_dir = root / "work"
        staging_dir = root / "staging"
        reports_dir = root / "reports"
        artifacts_zip = root / "artifacts.zip"

        download_s3(job["submission_s3_key"], submission_zip)
        safe_extract_zip(submission_zip, submission_dir)
        copy_sdk(root / "sdk")
        stage_task_workdirs(job["dataset_s3_key"], job["task_id"], staging_dir, work_dir, ("generate", "evaluate"))
        reports_dir.mkdir(parents=True, exist_ok=True)

        artifact_prefix = f"jobs/{job['id']}/generation/artifacts"
        preview_prefix = f"jobs/{job['id']}/generation/preview"

        write_container_script(root, "generation")
        generation_runtime = run_docker(root, job, phase="generation")
        if generation_runtime["returncode"] != 0:
            runtime_files = upload_runtime_files(job["id"], reports_dir, work_dir)
            update_job_runtime_metadata(job["id"], generation_runtime, runtime_files)
            raise RuntimeError(f"Docker generation failed with exit {generation_runtime['returncode']}:\n{generation_runtime['log_tail']}")

        make_generation_artifacts_zip(work_dir, artifacts_zip)
        preview_s3_key = None
        upload_s3_file(artifacts_zip, f"{artifact_prefix}.zip", "application/zip")
        preview_dist = work_dir / "generate" / "dist"
        if (preview_dist / "index.html").exists():
            upload_s3_directory(preview_dist, preview_prefix)
            preview_s3_key = f"{preview_prefix}/index.html"
        update_job_artifact_metadata(job["id"], artifact_prefix, preview_s3_key)

        write_container_script(root, "evaluation")
        evaluation_runtime = run_docker(root, job, phase="evaluation", artifact_url=_job_preview_url(job["id"]))
        runtime = combine_runtimes(generation_runtime, evaluation_runtime)
        runtime_files = upload_runtime_files(job["id"], reports_dir, work_dir)
        update_job_runtime_metadata(job["id"], runtime, runtime_files)
        if evaluation_runtime["returncode"] != 0:
            raise RuntimeError(f"Docker evaluation failed with exit {evaluation_runtime['returncode']}:\n{evaluation_runtime['log_tail']}")

        evaluation = json.loads((work_dir / "evaluate" / "evaluation.json").read_text(encoding="utf-8"))
        return {
            "result": evaluation,
            "score": evaluation.get("score"),
            "artifact_s3_prefix": artifact_prefix,
            "preview_s3_key": preview_s3_key,
            **runtime,
            **runtime_files,
        }


def run_peer_review_job(job: dict[str, Any]) -> dict[str, Any]:
    if not job.get("review_target_job_id"):
        raise RuntimeError("Peer review target job is not set")
    if not job.get("target_preview_s3_key"):
        raise RuntimeError("Peer review target preview is not available")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        submission_zip = root / "submission.zip"
        submission_dir = root / "submission"
        work_dir = root / "work"
        staging_dir = root / "staging"
        reports_dir = root / "reports"

        download_s3(job["submission_s3_key"], submission_zip)
        safe_extract_zip(submission_zip, submission_dir)
        copy_sdk(root / "sdk")
        stage_task_workdirs(job["dataset_s3_key"], job["task_id"], staging_dir, work_dir, ("evaluate",))
        reports_dir.mkdir(parents=True, exist_ok=True)

        write_container_script(root, "evaluation")
        runtime = run_docker(root, job, phase="evaluation", artifact_url=_job_preview_url(job["review_target_job_id"]))
        runtime_files = upload_runtime_files(job["id"], reports_dir, work_dir)
        update_job_runtime_metadata(job["id"], runtime, runtime_files)
        if runtime["returncode"] != 0:
            raise RuntimeError(f"Docker evaluation failed with exit {runtime['returncode']}:\n{runtime['log_tail']}")

        evaluation = json.loads((work_dir / "evaluate" / "evaluation.json").read_text(encoding="utf-8"))
        return {
            "result": evaluation,
            "score": evaluation.get("score"),
            **runtime,
            **runtime_files,
        }


def stage_task_workdirs(dataset_s3_key: str, task_id: str, staging_dir: Path, work_dir: Path, phases: tuple[str, ...]) -> None:
    task_root = copy_task_data(dataset_s3_key, task_id, staging_dir)
    for phase in phases:
        phase_workdir = work_dir / phase
        phase_workdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_root / "task.md", phase_workdir / "task.md")
        if (task_root / "data").exists():
            shutil.copytree(task_root / "data", phase_workdir / "data", dirs_exist_ok=True)


def copy_sdk(target: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    shutil.copytree(
        repo_root / "packages" / "arena-sdk",
        target,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.egg-info"),
    )


def make_generation_artifacts_zip(work_dir: Path, target_zip: Path) -> None:
    generate_dir = work_dir / "generate"
    if not generate_dir.exists():
        raise RuntimeError("Agent did not create a generate workdir")

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "output"
        staging.mkdir()
        for relative in ("source", "dist", "generation.json"):
            src = generate_dir / relative
            if not src.exists():
                continue
            dst = staging / relative
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        make_zip(staging, target_zip)


def write_container_script(root: Path, phase: str) -> None:
    script = render_container_script(phase)
    path = root / "run.sh"
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)


def render_container_script(phase: str) -> str:
    if phase not in {"generation", "evaluation"}:
        raise ValueError(f"Unknown container phase: {phase}")
    return """#!/usr/bin/env bash
set -euo pipefail
mkdir -p /arena/home /arena/.uv-cache /arena/.venv /arena/reports/generation /arena/reports/evaluation
export PATH="/arena/home/.local/bin:$PATH"
trace_event() {
  if [ "${VIS_ARENA_RECORD_TRAJECTORY:-true}" != "true" ]; then
    return 0
  fi
  python - "$1" "$2" "$3" "${4:-}" <<'PY'
import datetime
import json
import pathlib
import sys

path, event_type, phase, step = sys.argv[1:5]
event = {
    "type": event_type,
    "phase": phase,
    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
}
if step:
    event["step"] = step
pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, separators=(",", ":")) + "\\n")
PY
}
trace_manifest() {
  if [ "${VIS_ARENA_RECORD_TRAJECTORY:-true}" != "true" ]; then
    return 0
  fi
  python - "$1" "$2" "$3" <<'PY'
import datetime
import json
import pathlib
import sys

trace_path = pathlib.Path(sys.argv[1])
phase = sys.argv[2]
output_root = pathlib.Path(sys.argv[3])
files = []
if output_root.exists():
    for path in sorted(output_root.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(output_root).as_posix(), "size_bytes": path.stat().st_size})
event = {
    "type": "file_manifest",
    "phase": phase,
    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    "files": files,
}
trace_path.parent.mkdir(parents=True, exist_ok=True)
with open(trace_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, separators=(",", ":")) + "\\n")
PY
}
run_phase() {
  phase="$1"
  step="$2"
  shift 2
  phase_dir="/arena/reports/${phase}"
  log="${phase_dir}/runtime.log"
  trace="${phase_dir}/trajectory.jsonl"
  mkdir -p "$phase_dir"
  if [ ! -f "$log" ]; then
    echo "[$(date -Iseconds)] phase_start ${phase}" > "$log"
    trace_event "$trace" phase_start "$phase"
  fi
  echo "[$(date -Iseconds)] step_start ${phase}.${step}" >> "$log"
  trace_event "$trace" step_start "$phase" "$step"
  set +e
  "$@" >> "$log" 2>&1
  code=$?
  set -e
  echo "[$(date -Iseconds)] step_end ${phase}.${step} exit_code=${code}" >> "$log"
  trace_event "$trace" step_end "$phase" "$step"
  if [ "$phase" = "generation" ] && [ "$step" = "generate" ]; then
    trace_manifest "$trace" generation /arena/work/generate
  fi
  return "$code"
}
finish_phase() {
  phase="$1"
  phase_dir="/arena/reports/${phase}"
  log="${phase_dir}/runtime.log"
  trace="${phase_dir}/trajectory.jsonl"
  echo "[$(date -Iseconds)] phase_end ${phase}" >> "$log"
  trace_event "$trace" phase_end "$phase"
}
cd /arena/submission
if [ "${VIS_ARENA_PHASE}" = "generation" ]; then
  if [ -f pyproject.toml ]; then
    python -m pip install --upgrade pip uv >/tmp/pip.log 2>&1 || cat /tmp/pip.log
    run_phase generation info uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py info --output /arena/reports/generation/agent-info.json
    run_phase generation generate uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py generate /arena/work/generate
  else
    run_phase generation info ./agent info --output /arena/reports/generation/agent-info.json
    run_phase generation generate ./agent generate /arena/work/generate
  fi
  finish_phase generation
else
  if [ -f pyproject.toml ]; then
    python -m pip install --upgrade pip uv >/tmp/pip.log 2>&1 || cat /tmp/pip.log
    run_phase evaluation evaluate uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py evaluate /arena/work/evaluate
  else
    run_phase evaluation evaluate ./agent evaluate /arena/work/evaluate
  fi
  finish_phase evaluation
fi
"""


def run_docker(root: Path, job: dict[str, Any], *, phase: str, artifact_url: str | None = None) -> dict[str, Any]:
    arena_token = create_token(job["owner_id"])
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        settings.evaluator_network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-e",
        "HOME=/arena/home",
        "-e",
        "UV_CACHE_DIR=/arena/.uv-cache",
        "-e",
        "UV_PROJECT_ENVIRONMENT=/arena/.venv",
        "-e",
        f"VIS_ARENA_RECORD_TRAJECTORY={'true' if settings.record_trajectory else 'false'}",
        "-e",
        f"VIS_ARENA_JOB_TYPE={job.get('job_type') or 'generation'}",
        "-e",
        f"VIS_ARENA_SERVER_URL={_container_server_url()}",
        "-e",
        f"VIS_ARENA_API_TOKEN={arena_token}",
        "-e",
        f"VIS_ARENA_JOB_ID={job['id']}",
        "-e",
        f"VIS_ARENA_LLM_PROVIDER={settings.llm_provider}",
        "-e",
        f"VIS_ARENA_LLM_MODEL={settings.bedrock_default_model_id if settings.llm_provider == 'bedrock' else os.environ.get('VIS_ARENA_OPENAI_MODEL', 'gpt-4.1-mini')}",
        "-e",
        f"VIS_ARENA_LLM_MODELS={','.join(settings.bedrock_model_ids) if settings.llm_provider == 'bedrock' else os.environ.get('VIS_ARENA_OPENAI_MODEL', 'gpt-4.1-mini')}",
        "-e",
        f"VIS_ARENA_PHASE={phase}",
        "-v",
        f"{root}:/arena",
        "-w",
        "/arena",
        settings.evaluator_image,
        "bash",
        "/arena/run.sh",
    ]
    if artifact_url:
        cmd[cmd.index("-v"):cmd.index("-v")] = ["-e", f"VIS_ARENA_ARTIFACT_URL={artifact_url}"]
    started_at = now_iso()
    started = time.monotonic()
    try:
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=settings.evaluator_timeout_seconds)
        output = completed.stdout
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        output += f"\nDocker evaluation timed out after {settings.evaluator_timeout_seconds} seconds."
        returncode = 124
    completed_at = now_iso()
    run_seconds = round(time.monotonic() - started, 3)
    (root / "reports" / "docker.log").write_text(output, encoding="utf-8")
    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "run_seconds": run_seconds,
        "returncode": returncode,
        "log_tail": output[-4000:],
    }


def combine_runtimes(*runtimes: dict[str, Any]) -> dict[str, Any]:
    return {
        "started_at": runtimes[0]["started_at"],
        "completed_at": runtimes[-1]["completed_at"],
        "run_seconds": round(sum(float(runtime["run_seconds"]) for runtime in runtimes), 3),
        "returncode": runtimes[-1]["returncode"],
        "log_tail": "\n".join(str(runtime["log_tail"]) for runtime in runtimes)[-4000:],
    }


def upload_runtime_files(job_id: str, reports_dir: Path, work_dir: Path) -> dict[str, str | None]:
    generation_s3_prefix = f"jobs/{job_id}/generation"
    evaluation_s3_prefix = f"jobs/{job_id}/evaluation"
    agent_info_s3_key = None
    generation_trajectory_s3_key = None
    evaluation_trajectory_s3_key = None
    evaluation_report_s3_key = None

    generation_log = reports_dir / "generation" / "runtime.log"
    if generation_log.exists():
        upload_s3_file(generation_log, f"{generation_s3_prefix}/runtime.log", "text/plain")

    generation_trajectory = reports_dir / "generation" / "trajectory.jsonl"
    if generation_trajectory.exists():
        generation_trajectory_s3_key = f"{generation_s3_prefix}/trajectory.jsonl"
        upload_s3_file(generation_trajectory, generation_trajectory_s3_key, "application/x-ndjson")

    agent_info = reports_dir / "generation" / "agent-info.json"
    if agent_info.exists():
        agent_info_s3_key = f"{generation_s3_prefix}/agent-info.json"
        upload_s3_file(agent_info, agent_info_s3_key, "application/json")

    evaluation_log = reports_dir / "evaluation" / "runtime.log"
    if evaluation_log.exists():
        upload_s3_file(evaluation_log, f"{evaluation_s3_prefix}/runtime.log", "text/plain")

    evaluation_trajectory = reports_dir / "evaluation" / "trajectory.jsonl"
    if evaluation_trajectory.exists():
        evaluation_trajectory_s3_key = f"{evaluation_s3_prefix}/trajectory.jsonl"
        upload_s3_file(evaluation_trajectory, evaluation_trajectory_s3_key, "application/x-ndjson")

    evaluation_report = work_dir / "evaluate" / "evaluation.json"
    if evaluation_report.exists():
        evaluation_report_s3_key = f"{evaluation_s3_prefix}/report.json"
        upload_s3_file(evaluation_report, evaluation_report_s3_key, "application/json")

    return {
        "generation_s3_prefix": generation_s3_prefix,
        "evaluation_s3_prefix": evaluation_s3_prefix,
        "agent_info_s3_key": agent_info_s3_key,
        "generation_trajectory_s3_key": generation_trajectory_s3_key,
        "evaluation_trajectory_s3_key": evaluation_trajectory_s3_key,
        "evaluation_report_s3_key": evaluation_report_s3_key,
    }


def _container_server_url() -> str:
    if settings.public_base_url in {"http://localhost:8000", "http://127.0.0.1:8000"}:
        return "http://host.docker.internal:8000"
    return settings.public_base_url


def _job_preview_url(job_id: str) -> str:
    return f"{_container_server_url()}/v1/jobs/{job_id}/preview"


def update_job_artifact_metadata(job_id: str, artifact_s3_prefix: str, preview_s3_key: str | None) -> None:
    now = now_iso()
    with connect() as db:
        db.execute(
            """
            update jobs
            set artifact_s3_prefix = ?, preview_s3_key = ?, updated_at = ?
            where id = ?
            """,
            (artifact_s3_prefix, preview_s3_key, now, job_id),
        )


def refresh_waiting_reviews() -> None:
    now = now_iso()
    with connect() as db:
        waiting = db.execute(
            """
            select jobs.*
            from jobs join submissions reviewer on reviewer.id = jobs.submission_id
            where jobs.job_type = 'peer_review'
              and jobs.status = 'waiting_reviewer'
              and (reviewer.reviewer_eligible_at is not null or reviewer.status = 'failed')
            order by jobs.created_at
            """
        ).fetchall()
        for row in waiting:
            review = dict(row)
            reviewer = db.execute("select * from submissions where id = ?", (review["submission_id"],)).fetchone()
            if reviewer is not None and reviewer["reviewer_eligible_at"] and reviewer["status"] != "failed":
                db.execute("update jobs set status = ?, updated_at = ? where id = ?", ("queued", now, review["id"]))
            else:
                _fallback_waiting_review(db, review, now)
            _update_generator_submission_rollup(db, review["generator_submission_id"], now)


def queue_peer_reviews_for_generation(db, generation_job_id: str, cutoff_at: str) -> None:
    generation_job = db.execute(
        """
        select jobs.*, submissions.owner_id as generator_owner_id
        from jobs join submissions on submissions.id = coalesce(jobs.generator_submission_id, jobs.submission_id)
        where jobs.id = ?
        """,
        (generation_job_id,),
    ).fetchone()
    if generation_job is None:
        return
    job = dict(generation_job)
    generator_submission_id = job["generator_submission_id"] or job["submission_id"]
    reviewer_rows = db.execute(
        """
        select id, owner_id, status, finalized_at, reviewer_eligible_at, created_at
        from submissions
        where owner_id != ?
          and finalized_at is not null
          and finalized_at <= ?
        order by owner_id, finalized_at desc, created_at desc
        """,
        (job["generator_owner_id"], cutoff_at),
    ).fetchall()

    latest_by_user: dict[str, dict[str, Any]] = {}
    for row in reviewer_rows:
        reviewer = dict(row)
        latest_by_user.setdefault(reviewer["owner_id"], reviewer)

    for reviewer in latest_by_user.values():
        already_exists = db.execute(
            """
            select 1 from jobs
            where job_type = 'peer_review'
              and review_target_job_id = ?
              and reviewer_user_id = ?
            """,
            (generation_job_id, reviewer["owner_id"]),
        ).fetchone()
        if already_exists:
            continue

        reviewer_submission_id = reviewer["id"]
        status = "waiting_reviewer"
        error = None
        if reviewer["status"] == "failed":
            fallback = _previous_eligible_reviewer_submission(db, reviewer["owner_id"], cutoff_at, reviewer["id"])
            if fallback:
                reviewer_submission_id = fallback["id"]
                status = "queued"
            else:
                status = "failed"
                error = "No eligible reviewer submission is available for this user"
        elif reviewer["reviewer_eligible_at"]:
            status = "queued"

        db.execute(
            """
            insert into jobs (
              id, submission_id, job_type, generator_submission_id,
              review_target_job_id, reviewer_user_id, reviewer_cutoff_at,
              dataset_id, task_id, status, error, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                reviewer_submission_id,
                "peer_review",
                generator_submission_id,
                generation_job_id,
                reviewer["owner_id"],
                cutoff_at,
                job["dataset_id"],
                job["task_id"],
                status,
                error,
                cutoff_at,
                cutoff_at,
            ),
        )


def _fallback_waiting_review(db, review: dict[str, Any], now: str) -> None:
    fallback = _previous_eligible_reviewer_submission(
        db,
        review["reviewer_user_id"],
        review["reviewer_cutoff_at"],
        review["submission_id"],
    )
    if fallback:
        db.execute(
            "update jobs set submission_id = ?, status = ?, error = null, updated_at = ? where id = ?",
            (fallback["id"], "queued", now, review["id"]),
        )
        return
    db.execute(
        "update jobs set status = ?, error = ?, updated_at = ? where id = ?",
        ("failed", "Reviewer submission failed and no previous eligible submission is available", now, review["id"]),
    )


def _previous_eligible_reviewer_submission(db, reviewer_user_id: str, cutoff_at: str, exclude_submission_id: str | None) -> dict[str, Any] | None:
    row = db.execute(
        """
        select *
        from submissions
        where owner_id = ?
          and finalized_at is not null
          and finalized_at <= ?
          and reviewer_eligible_at is not null
          and status != 'failed'
          and (? is null or id != ?)
        order by finalized_at desc, created_at desc
        limit 1
        """,
        (reviewer_user_id, cutoff_at, exclude_submission_id, exclude_submission_id),
    ).fetchone()
    return row_to_dict(row)


def _update_generator_submission_rollup(db, submission_id: str | None, now: str) -> None:
    if not submission_id:
        return

    generation_summary = db.execute(
        """
        select
          count(*) as total,
          sum(case when status in ('queued', 'running') then 1 else 0 end) as pending,
          sum(case when status = 'succeeded' then 1 else 0 end) as succeeded
        from jobs
        where coalesce(job_type, 'generation') = 'generation'
          and submission_id = ?
        """,
        (submission_id,),
    ).fetchone()
    total_generations = int(generation_summary["total"] or 0)
    pending_generations = int(generation_summary["pending"] or 0)
    succeeded_generations = int(generation_summary["succeeded"] or 0)
    if total_generations == 0:
        return

    if pending_generations == 0:
        submission = db.execute("select status, reviewer_eligible_at from submissions where id = ?", (submission_id,)).fetchone()
        if succeeded_generations > 0:
            if submission is not None and not submission["reviewer_eligible_at"]:
                db.execute("update submissions set reviewer_eligible_at = ? where id = ?", (now, submission_id))
                waiting_reviews = db.execute(
                    """
                    select * from jobs
                    where job_type = 'peer_review'
                      and submission_id = ?
                      and status = 'waiting_reviewer'
                    """,
                    (submission_id,),
                ).fetchall()
                for row in waiting_reviews:
                    db.execute("update jobs set status = ?, updated_at = ? where id = ?", ("queued", now, row["id"]))
        else:
            db.execute("update submissions set status = ?, score = ? where id = ?", ("failed", None, submission_id))
            waiting_reviews = db.execute(
                """
                select * from jobs
                where job_type = 'peer_review'
                  and submission_id = ?
                  and status = 'waiting_reviewer'
                """,
                (submission_id,),
            ).fetchall()
            for row in waiting_reviews:
                review = dict(row)
                _fallback_waiting_review(db, review, now)
                _update_generator_submission_rollup(db, review["generator_submission_id"], now)
            return

    pending_reviews = db.execute(
        """
        select count(*) as count
        from jobs
        where job_type in ('peer_review', 'peer_evaluation', 'central_evaluation')
          and generator_submission_id = ?
          and status in ('queued', 'running', 'waiting_reviewer')
        """,
        (submission_id,),
    ).fetchone()["count"]

    if pending_generations == 0 and succeeded_generations > 0 and pending_reviews == 0:
        peer_scores = _job_scores(
            db,
            """
            select json_extract(result_json, '$.score') as score
            from jobs
            where job_type in ('peer_review', 'peer_evaluation')
              and generator_submission_id = ?
              and status = 'succeeded'
            """,
            (submission_id,),
        )
        generation_scores = _job_scores(
            db,
            """
            select json_extract(result_json, '$.score') as score
            from jobs
            where coalesce(job_type, 'generation') = 'generation'
              and submission_id = ?
              and status = 'succeeded'
            """,
            (submission_id,),
        )
        scores = peer_scores or generation_scores
        average = sum(scores) / len(scores) if scores else None
        db.execute("update submissions set status = ?, score = ? where id = ?", ("succeeded", average, submission_id))
    elif succeeded_generations > 0 or pending_generations > 0:
        db.execute("update submissions set status = ? where id = ?", ("running", submission_id))


def _job_scores(db, query: str, params: tuple[str, ...]) -> list[float]:
    return [float(row["score"]) for row in db.execute(query, params) if row["score"] is not None]


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    now = now_iso()
    round_to_check = None
    with connect() as db:
        job = row_to_dict(db.execute("select * from jobs where id = ?", (job_id,)).fetchone())
        if not job:
            return
        job_type = job.get("job_type") or "generation"
        db.execute(
            """
            update jobs
            set status = ?, result_json = ?, artifact_s3_prefix = ?, preview_s3_key = ?,
                generation_s3_prefix = ?, evaluation_s3_prefix = ?,
                agent_info_s3_key = ?, generation_trajectory_s3_key = ?,
                evaluation_trajectory_s3_key = ?, evaluation_report_s3_key = ?,
                started_at = ?, completed_at = ?, run_seconds = ?, updated_at = ?
            where id = ?
            """,
            (
                "succeeded",
                json.dumps(result.get("result")),
                result.get("artifact_s3_prefix"),
                result.get("preview_s3_key"),
                result.get("generation_s3_prefix"),
                result.get("evaluation_s3_prefix"),
                result.get("agent_info_s3_key"),
                result.get("generation_trajectory_s3_key"),
                result.get("evaluation_trajectory_s3_key"),
                result.get("evaluation_report_s3_key"),
                result.get("started_at"),
                result.get("completed_at"),
                result.get("run_seconds"),
                now,
                job_id,
            ),
        )
        generator_submission_id = job["generator_submission_id"] or job["submission_id"]
        if job_type == "generation":
            write_self_evaluation_for_generation(db, job, result, now)
            queue_central_evaluation_for_generation(db, job_id, now)
            if not job.get("round_id") and not settings.rounds_enabled:
                queue_peer_reviews_for_generation(db, job_id, now)
        elif job_type in EVALUATION_JOB_TYPES:
            write_evaluation_job_result(db, job, result, now)
            if job.get("round_id"):
                round_to_check = job["round_id"]
        _update_generator_submission_rollup(db, generator_submission_id, now)
    if round_to_check:
        complete_round_if_ready(round_to_check)


def update_job_runtime_metadata(job_id: str, runtime: dict[str, Any], runtime_files: dict[str, str | None]) -> None:
    now = now_iso()
    with connect() as db:
        db.execute(
            """
            update jobs
            set generation_s3_prefix = ?, evaluation_s3_prefix = ?,
                agent_info_s3_key = ?, generation_trajectory_s3_key = ?,
                evaluation_trajectory_s3_key = ?, evaluation_report_s3_key = ?,
                started_at = ?, completed_at = ?, run_seconds = ?, updated_at = ?
            where id = ?
            """,
            (
                runtime_files["generation_s3_prefix"],
                runtime_files["evaluation_s3_prefix"],
                runtime_files["agent_info_s3_key"],
                runtime_files["generation_trajectory_s3_key"],
                runtime_files["evaluation_trajectory_s3_key"],
                runtime_files["evaluation_report_s3_key"],
                runtime["started_at"],
                runtime["completed_at"],
                runtime["run_seconds"],
                now,
                job_id,
            ),
        )


def fail_job(job_id: str, exc: Exception) -> None:
    now = now_iso()
    round_to_check = None
    with connect() as db:
        job = row_to_dict(db.execute("select * from jobs where id = ?", (job_id,)).fetchone())
        db.execute("update jobs set status = ?, error = ?, updated_at = ? where id = ?", ("failed", str(exc), now, job_id))
        if job:
            write_evaluation_job_failure(db, job, str(exc), now)
            if job.get("round_id"):
                round_to_check = job["round_id"]
            _update_generator_submission_rollup(db, job["generator_submission_id"] or job["submission_id"], now)
    if round_to_check:
        complete_round_if_ready(round_to_check)
