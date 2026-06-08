from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .auth import create_token
from .db import connect, now_iso, row_to_dict
from .settings import settings
from .storage import copy_task_data, download_s3, make_zip, safe_extract_zip, upload_s3_directory, upload_s3_file


def run() -> None:
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


def claim_job() -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            """
            select jobs.*, submissions.owner_id, submissions.s3_key as submission_s3_key, datasets.s3_key as dataset_s3_key
            from jobs
            join submissions on submissions.id = jobs.submission_id
            join datasets on datasets.id = jobs.dataset_id
            where jobs.status = 'queued'
            order by jobs.created_at
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        now = now_iso()
        db.execute("update jobs set status = ?, updated_at = ? where id = ?", ("running", now, row["id"]))
        db.execute("update submissions set status = ? where id = ?", ("running", row["submission_id"]))
        return dict(row)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        submission_zip = root / "submission.zip"
        submission_dir = root / "submission"
        work_dir = root / "work"            # holds generate/ and evaluate/ workdirs
        staging_dir = root / "staging"      # extracted task (task.md + data/)
        reports_dir = root / "reports"
        artifacts_zip = root / "artifacts.zip"

        download_s3(job["submission_s3_key"], submission_zip)
        safe_extract_zip(submission_zip, submission_dir)
        copy_sdk(root / "sdk")

        # Extract the task once, then stage task.md + data/ into both workdirs.
        # Each phase gets a fresh workdir; the evaluate workdir does NOT get source/.
        task_root = copy_task_data(job["dataset_s3_key"], job["task_id"], staging_dir)
        for phase in ("generate", "evaluate"):
            phase_workdir = work_dir / phase
            phase_workdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(task_root / "task.md", phase_workdir / "task.md")
            if (task_root / "data").exists():
                shutil.copytree(task_root / "data", phase_workdir / "data", dirs_exist_ok=True)

        reports_dir.mkdir(parents=True, exist_ok=True)

        script = render_container_script()
        (root / "run.sh").write_text(script, encoding="utf-8")
        os.chmod(root / "run.sh", 0o755)
        runtime = run_docker(root, job)
        runtime_files = upload_runtime_files(job["id"], reports_dir, work_dir)
        update_job_runtime_metadata(job["id"], runtime, runtime_files)
        if runtime["returncode"] != 0:
            raise RuntimeError(f"Docker evaluation failed with exit {runtime['returncode']}:\n{runtime['log_tail']}")

        evaluation = json.loads((work_dir / "evaluate" / "evaluation.json").read_text(encoding="utf-8"))
        make_generation_artifacts_zip(work_dir, artifacts_zip)
        artifact_prefix = f"jobs/{job['id']}/generation/artifacts"
        preview_prefix = f"jobs/{job['id']}/generation/preview"
        preview_s3_key = None
        upload_s3_file(artifacts_zip, f"{artifact_prefix}.zip", "application/zip")
        preview_dist = work_dir / "generate" / "dist"
        if (preview_dist / "index.html").exists():
            upload_s3_directory(preview_dist, preview_prefix)
            preview_s3_key = f"{preview_prefix}/index.html"
        return {
            "evaluation": evaluation,
            "score": evaluation.get("score"),
            "artifact_s3_prefix": artifact_prefix,
            "preview_s3_key": preview_s3_key,
            **runtime,
            **runtime_files,
        }


def copy_sdk(target: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    shutil.copytree(
        repo_root / "packages" / "arena-sdk",
        target,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.egg-info"),
    )


def make_generation_artifacts_zip(work_dir: Path, target_zip: Path) -> None:
    """Zip the generation outputs: source/, dist/, generation.json.

    Excludes task.md and data/, which are inputs staged by the worker rather
    than agent-produced artifacts.
    """
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


def render_container_script() -> str:
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
stage_dist_for_evaluate() {
  # After generate completes, mirror dist/ into the evaluate workdir so the
  # agent.py contract can serve it locally during evaluate.
  if [ -d /arena/work/generate/dist ]; then
    rm -rf /arena/work/evaluate/dist
    cp -r /arena/work/generate/dist /arena/work/evaluate/dist
  fi
}
cd /arena/submission
if [ -f pyproject.toml ]; then
  python -m pip install --upgrade pip uv >/tmp/pip.log 2>&1 || cat /tmp/pip.log
  run_phase generation info uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py info --output /arena/reports/generation/agent-info.json
  run_phase generation generate uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py generate /arena/work/generate
  finish_phase generation
  stage_dist_for_evaluate
  run_phase evaluation evaluate uv run --with-editable /arena/sdk --with-editable . python /arena/submission/agent.py evaluate /arena/work/evaluate
  finish_phase evaluation
else
  run_phase generation info ./agent info --output /arena/reports/generation/agent-info.json
  run_phase generation generate ./agent generate /arena/work/generate
  finish_phase generation
  stage_dist_for_evaluate
  run_phase evaluation evaluate ./agent evaluate /arena/work/evaluate
  finish_phase evaluation
fi
"""


def run_docker(root: Path, job: dict[str, Any]) -> dict[str, Any]:
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
        "-v",
        f"{root}:/arena",
        "-w",
        "/arena",
        settings.evaluator_image,
        "bash",
        "/arena/run.sh",
    ]
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


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    now = now_iso()
    with connect() as db:
        job = db.execute("select submission_id from jobs where id = ?", (job_id,)).fetchone()
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
                json.dumps(result["evaluation"]),
                result["artifact_s3_prefix"],
                result["preview_s3_key"],
                result["generation_s3_prefix"],
                result["evaluation_s3_prefix"],
                result["agent_info_s3_key"],
                result["generation_trajectory_s3_key"],
                result["evaluation_trajectory_s3_key"],
                result["evaluation_report_s3_key"],
                result["started_at"],
                result["completed_at"],
                result["run_seconds"],
                now,
                job_id,
            ),
        )
        scores = [
            row["score"]
            for row in db.execute("select json_extract(result_json, '$.score') as score from jobs where submission_id = ? and status = 'succeeded'", (job["submission_id"],))
            if row["score"] is not None
        ]
        remaining = db.execute("select count(*) as count from jobs where submission_id = ? and status in ('queued', 'running')", (job["submission_id"],)).fetchone()["count"]
        if remaining == 0:
            avg = sum(float(score) for score in scores) / len(scores) if scores else None
            status = "succeeded" if scores else "failed"
            db.execute("update submissions set status = ?, score = ? where id = ?", (status, avg, job["submission_id"]))


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
    with connect() as db:
        job = row_to_dict(db.execute("select submission_id from jobs where id = ?", (job_id,)).fetchone())
        db.execute("update jobs set status = ?, error = ?, updated_at = ? where id = ?", ("failed", str(exc), now, job_id))
        if job:
            remaining = db.execute("select count(*) as count from jobs where submission_id = ? and status in ('queued', 'running')", (job["submission_id"],)).fetchone()["count"]
            if remaining == 0:
                db.execute("update submissions set status = ? where id = ?", ("failed", job["submission_id"]))
