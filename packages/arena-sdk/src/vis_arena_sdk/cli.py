from __future__ import annotations

import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import socketserver
import threading
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Optional

import typer

from .client import VisArenaClient, VisArenaError
from .config import load_config, resolve_server_url, resolve_token, save_config

app = typer.Typer(help="Vis Arena command line client")
datasets_app = typer.Typer(help="Dataset commands")
submissions_app = typer.Typer(help="Submission commands")
results_app = typer.Typer(help="Result commands")
local_app = typer.Typer(help="Local agent development commands")
llm_app = typer.Typer(help="Cloud LLM token commands")
admin_app = typer.Typer(help="Admin commands")
admin_rounds_app = typer.Typer(help="Peer-review round admin commands")
app.add_typer(datasets_app, name="datasets")
app.add_typer(submissions_app, name="submissions")
app.add_typer(results_app, name="results")
app.add_typer(local_app, name="local")
app.add_typer(llm_app, name="llm")
app.add_typer(admin_app, name="admin")
admin_app.add_typer(admin_rounds_app, name="rounds")


@app.command()
def init(
    directory: Path,
    force: bool = typer.Option(False, "--force", "-f", help="Scaffold even if the directory is not empty."),
) -> None:
    """Scaffold a new agent project from the Python template."""
    if directory.exists() and any(directory.iterdir()) and not force:
        typer.echo(f"Directory {directory} is not empty. Use --force to scaffold anyway.", err=True)
        raise typer.Exit(1)
    directory.mkdir(parents=True, exist_ok=True)

    template_root = resources.files("vis_arena_sdk").joinpath("templates", "python")
    for entry in template_root.iterdir():
        if not entry.is_file():
            continue
        out_path = directory / entry.name
        out_path.write_bytes(entry.read_bytes())
        if entry.name == "agent.py":
            out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    typer.echo(f"Scaffolded agent template in {directory}")
    typer.echo(f"  Next: cd {directory}")
    typer.echo("        edit agent.py, then test locally with your OPENAI_API_KEY")


def _client(server_url: str | None = None, token: str | None = None) -> VisArenaClient:
    return VisArenaClient(base_url=resolve_server_url(server_url), token=resolve_token(token))


def _cli_version() -> str:
    try:
        return package_version("vis-arena-sdk")
    except PackageNotFoundError:
        return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in value.split("."):
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits or "0"))
    return tuple(parts)


def _is_less_than(left: str, right: str) -> bool:
    left_parts = _version_tuple(left)
    right_parts = _version_tuple(right)
    max_len = max(len(left_parts), len(right_parts))
    return left_parts + (0,) * (max_len - len(left_parts)) < right_parts + (0,) * (max_len - len(right_parts))


def _check_cli_version(client: VisArenaClient, *, enforce_minimum: bool = False) -> None:
    try:
        remote = client.version()
    except VisArenaError:
        return
    current = _cli_version()
    latest = str(remote.get("latest_cli_version") or "")
    minimum = str(remote.get("minimum_cli_version") or "")
    update_command = str(remote.get("update_command") or "")
    if minimum and _is_less_than(current, minimum):
        typer.echo(
            f"Vis Arena CLI {current} is no longer supported. Please update to {minimum} or newer.",
            err=True,
        )
        if update_command:
            typer.echo(f"Update with:\n{update_command}", err=True)
        if enforce_minimum:
            raise typer.Exit(2)
    elif latest and _is_less_than(current, latest):
        typer.echo(f"Update available: Vis Arena CLI {current} -> {latest}", err=True)
        if update_command:
            typer.echo(f"Update with:\n{update_command}", err=True)


@app.command()
def register(email: str, password: str, name: Optional[str] = None, server_url: Optional[str] = None) -> None:
    """Create an arena account and store the API token."""
    client = _client(server_url, None)
    try:
        _check_cli_version(client, enforce_minimum=True)
        auth = client.register(email, password, name)
    finally:
        client.close()
    config = load_config() | {"server_url": resolve_server_url(server_url), "access_token": auth.access_token}
    save_config(config)
    typer.echo(f"Registered {auth.user.email}")


@app.command()
def login(email: str, password: str, server_url: Optional[str] = None) -> None:
    """Log in and store the API token."""
    client = _client(server_url, None)
    try:
        _check_cli_version(client, enforce_minimum=True)
        auth = client.login(email, password)
    finally:
        client.close()
    config = load_config() | {"server_url": resolve_server_url(server_url), "access_token": auth.access_token}
    save_config(config)
    typer.echo(f"Logged in as {auth.user.email}")


@app.command()
def whoami(server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Show the authenticated user."""
    client = _client(server_url, token)
    try:
        _check_cli_version(client, enforce_minimum=True)
        typer.echo(client.me())
    finally:
        client.close()


@app.command()
def version(server_url: Optional[str] = None) -> None:
    """Show CLI and server version information."""
    client = VisArenaClient(base_url=resolve_server_url(server_url))
    try:
        typer.echo(f"CLI: {_cli_version()}")
        try:
            remote = client.version()
        except VisArenaError as exc:
            typer.echo(f"Server version unavailable: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Server: {remote.get('server_version', 'unknown')}")
        latest = remote.get("latest_cli_version")
        minimum = remote.get("minimum_cli_version")
        if latest:
            typer.echo(f"Latest CLI: {latest}")
        if minimum:
            typer.echo(f"Minimum CLI: {minimum}")
        _check_cli_version(client)
    finally:
        client.close()


@app.command()
def submit(
    path: Path,
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Submission name. Defaults to the file or folder name."),
    dataset_id: Optional[str] = typer.Option(None, "--dataset-id", "--dataset", help="Deprecated; submissions run against all active public datasets."),
    server_url: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """Upload an agent submission."""
    _upload_submission(path, name=name or _default_submission_name(path), dataset_id=dataset_id, server_url=server_url, token=token)


@datasets_app.command("list")
def datasets_list(server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """List datasets visible to the authenticated user."""
    client = _client(server_url, token)
    try:
        for dataset in client.list_datasets():
            typer.echo(f"{dataset.id}\t{dataset.name}\t{dataset.visibility}\t{dataset.task_count} tasks")
    finally:
        client.close()


@datasets_app.command("upload")
def datasets_upload(path: Path, name: str, visibility: str = "private", server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Upload a dataset/task ZIP bundle."""
    client = _client(server_url, token)
    try:
        dataset = client.upload_dataset(path, name=name, visibility=visibility)
        typer.echo(f"{dataset.id}\t{dataset.name}\t{dataset.visibility}")
    finally:
        client.close()


@datasets_app.command("download")
def datasets_download(dataset_id: str, output: Path, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Download a dataset bundle."""
    client = _client(server_url, token)
    try:
        dataset = client.resolve_dataset(dataset_id)
        path = client.download_dataset(dataset.id, output)
        typer.echo(str(path))
    finally:
        client.close()


@submissions_app.command("upload")
def submissions_upload(path: Path, name: str, dataset_id: Optional[str] = None, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Upload an agent submission ZIP."""
    _upload_submission(path, name=name, dataset_id=dataset_id, server_url=server_url, token=token)


def _default_submission_name(path: Path) -> str:
    """Pick a fallback name when --name is omitted.

    `path.stem` is "" for `Path(".")`, which would create a submission literally
    named "" on the leaderboard. Resolve first so submissions from inside a
    bundle dir use the dir name; strip a `.zip` suffix when present.
    """
    resolved = path.resolve()
    return resolved.stem if resolved.suffix == ".zip" else resolved.name


def _upload_submission(path: Path, name: str, dataset_id: Optional[str] = None, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    client = _client(server_url, token)
    try:
        submission = client.upload_submission(
            path, name=name, dataset_id=None
        )
        if dataset_id:
            typer.echo("--dataset is deprecated and ignored; submissions run against all active public datasets.", err=True)
        typer.echo(f"Submission {submission.id} queued for all active public datasets.")
        typer.echo(f"  Track progress:  vis-arena submissions watch {submission.id}")
        typer.echo(f"  Preview artifact: vis-arena submissions preview {submission.id}")
    finally:
        client.close()


@submissions_app.command("list")
def submissions_list(server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """List your submissions."""
    client = _client(server_url, token)
    try:
        for submission in client.list_submissions():
            score = "" if submission.score is None else f"{submission.score:.2f}"
            typer.echo(f"{submission.id}\t{submission.name}\t{submission.status}\t{score}")
    finally:
        client.close()


@submissions_app.command("usage")
def submissions_usage(submission_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Show LLM usage for a submission."""
    client = _client(server_url, token)
    try:
        typer.echo(json.dumps(client.get_submission_llm_usage(submission_id), indent=2))
    finally:
        client.close()


@submissions_app.command("watch")
def submissions_watch(
    submission_id: str,
    poll_seconds: float = typer.Option(10.0, "--poll-seconds", min=1.0, help="Seconds between status checks."),
    timeout_seconds: float = typer.Option(0.0, "--timeout-seconds", min=0.0, help="Stop after this many seconds. 0 means no timeout."),
    server_url: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """Poll a submission until it finishes."""
    client = _client(server_url, token)
    started = time.monotonic()
    last_line = None
    try:
        while True:
            submission = client.get_submission(submission_id)
            jobs = client.list_submission_jobs(submission_id)
            usage = client.get_submission_llm_usage(submission_id)
            line = _format_submission_status(submission.status, submission.score, jobs, usage)
            if line != last_line:
                typer.echo(line)
                last_line = line
            if submission.status in {"succeeded", "failed", "cancelled"}:
                if submission.status == "succeeded":
                    _print_submission_preview_urls(client, jobs)
                elif _job_errors(jobs):
                    typer.echo(_job_errors(jobs), err=True)
                return
            if timeout_seconds and time.monotonic() - started >= timeout_seconds:
                typer.echo(f"Timed out waiting for submission {submission_id}.", err=True)
                raise typer.Exit(2)
            time.sleep(poll_seconds)
    finally:
        client.close()


@submissions_app.command("preview")
def submissions_preview(submission_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Print HTML preview URL(s) for a finished submission."""
    client = _client(server_url, token)
    try:
        submission = client.get_submission(submission_id)
        jobs = client.list_submission_jobs(submission_id)
        preview_jobs = [job for job in jobs if job.get("preview_s3_key")]
        if preview_jobs:
            _print_submission_preview_urls(client, preview_jobs)
            return
        if submission.status in {"queued", "running", "uploading"} or any(job.get("status") in {"queued", "running", "waiting_reviewer"} for job in jobs):
            typer.echo(f"Submission {submission_id} is still {submission.status}.")
            typer.echo(f"  Track progress: vis-arena submissions watch {submission_id}")
            raise typer.Exit(2)
        errors = _job_errors(jobs)
        if errors:
            typer.echo(errors, err=True)
        raise VisArenaError(f"No preview artifact is available for submission {submission_id}.")
    finally:
        client.close()


@submissions_app.command("results")
def submissions_results(submission_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """List task-level results for a submission."""
    client = _client(server_url, token)
    try:
        for result in client.list_submission_jobs(submission_id):
            preview = "preview" if result.get("preview_s3_key") else ""
            run_seconds = result.get("run_seconds")
            duration = "" if run_seconds is None else f"{float(run_seconds):.1f}s"
            job_type = result.get("job_type") or "generation"
            typer.echo(f"{result['id']}\t{job_type}\t{result['task_id']}\t{result['status']}\t{duration}\t{preview}")
    finally:
        client.close()


def _format_submission_status(status: str, score: float | None, jobs: list[dict], usage: dict) -> str:
    generation_jobs = [job for job in jobs if (job.get("job_type") or "generation") == "generation"]
    review_jobs = [job for job in jobs if job.get("job_type") in {"peer_review", "peer_evaluation", "central_evaluation"}]
    run_seconds = max((float(job["run_seconds"]) for job in jobs if job.get("run_seconds") is not None), default=None)
    review_run_seconds = max((float(job["run_seconds"]) for job in review_jobs if job.get("run_seconds") is not None), default=None)
    total_tokens = int((usage.get("summary") or {}).get("total_tokens") or 0)
    parts = [status]
    if generation_jobs:
        parts.append("generation=" + _summarize_job_statuses(generation_jobs))
    if review_jobs:
        parts.append("reviews=" + _summarize_job_statuses(review_jobs).replace("waiting_reviewer", "waiting"))
    if run_seconds is not None:
        parts.append(f"runtime={run_seconds:.1f}s")
    if review_run_seconds is not None:
        parts.append(f"review_runtime={review_run_seconds:.1f}s")
    if total_tokens:
        parts.append(f"tokens={total_tokens:,}")
    if score is not None:
        parts.append(f"score={score:.2f}")
    return "  ".join(parts)


def _summarize_job_statuses(jobs: list[dict]) -> str:
    counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return ",".join(f"{status}:{counts[status]}" for status in sorted(counts))


def _print_submission_preview_urls(client: VisArenaClient, jobs: list[dict]) -> None:
    preview_jobs = [job for job in jobs if job.get("preview_s3_key")]
    if not preview_jobs:
        return
    for job in preview_jobs:
        prefix = f"{job.get('task_id')}: " if len(preview_jobs) > 1 and job.get("task_id") else ""
        typer.echo(prefix + client.get_job_preview_url(str(job["id"])))


def _job_errors(jobs: list[dict]) -> str:
    errors = [f"{job.get('task_id') or job.get('id')}: {job.get('error')}" for job in jobs if job.get("error")]
    return "\n".join(errors)


@local_app.command("run")
def local_run(
    agent_dir: Path = typer.Argument(Path("."), help="Agent bundle directory containing agent.py."),
    dataset_id: Optional[str] = typer.Option(None, "--dataset-id", "--dataset", help="Dataset id, name, or slug to test against."),
    task_path: Optional[Path] = typer.Option(None, "--task", "-t", help="Local task directory or dataset ZIP containing task.md and data/."),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o", help="Directory for the local run outputs."),
    env_file: Optional[Path] = typer.Option(None, "--env-file", help="Optional .env file for local provider keys."),
    server_url: Optional[str] = None,
    token: Optional[str] = None,
    force: bool = typer.Option(False, "--force", help="Replace an existing output directory."),
) -> None:
    """Run info, generate, and evaluate locally without uploading to the arena."""
    agent_dir = agent_dir.resolve()
    agent_py = agent_dir / "agent.py"
    if not agent_py.exists():
        raise VisArenaError(f"Missing agent entrypoint: {agent_py}")

    run_dir = (output_dir.resolve() if output_dir else agent_dir / ".vis-arena" / "local-runs" / _timestamp())
    if run_dir.exists() and any(run_dir.iterdir()):
        if not force:
            raise VisArenaError(f"Output directory is not empty: {run_dir}. Use --force to replace it.")
        shutil.rmtree(run_dir)
    generation_dir = run_dir / "generation"
    evaluation_dir = run_dir / "evaluation"
    generation_dir.mkdir(parents=True, exist_ok=True)
    evaluation_dir.mkdir(parents=True, exist_ok=True)

    task_dir = _resolve_local_task(dataset_id, task_path, run_dir, server_url, token)
    task_md = task_dir / "task.md"
    task_data = task_dir / "data"
    if not task_md.exists():
        raise VisArenaError(f"Missing task file: {task_md}")
    if not task_data.exists():
        raise VisArenaError(f"Missing task data directory: {task_data}")
    shutil.copy2(task_md, generation_dir / "task.md")
    shutil.copytree(task_data, generation_dir / "data", dirs_exist_ok=True)
    shutil.copy2(task_md, evaluation_dir / "task.md")

    env = _local_env(agent_dir, env_file)
    typer.echo(f"Local run: {run_dir}")
    _run_agent_command(agent_dir, ["info", "--output", str(run_dir / "agent-info.json")], run_dir / "info.log", env)
    _run_agent_command(agent_dir, ["generate", str(generation_dir)], run_dir / "generation.log", env)

    dist_dir = generation_dir / "dist"
    if not (dist_dir / "index.html").exists():
        raise VisArenaError(f"Generation did not create {dist_dir / 'index.html'}")

    eval_env = env.copy()
    with _serve_directory(dist_dir) as artifact_url:
        eval_env["VIS_ARENA_ARTIFACT_URL"] = artifact_url
        _run_agent_command(agent_dir, ["evaluate", str(evaluation_dir)], run_dir / "evaluation.log", eval_env)

    evaluation_json = evaluation_dir / "evaluation.json"
    score_text = ""
    if evaluation_json.exists():
        payload = json.loads(evaluation_json.read_text(encoding="utf-8"))
        score_text = f"  score: {payload.get('score')} / {payload.get('max_score')}"
    typer.echo("Local run succeeded.")
    if score_text:
        typer.echo(score_text)
    typer.echo(f"  artifact: {dist_dir / 'index.html'}")
    typer.echo(f"  logs: {run_dir}")
    typer.echo(f"  preview: vis-arena local preview {run_dir}")


@local_app.command("preview")
def local_preview(
    run_dir: Path = typer.Argument(..., help="Local run directory created by `vis-arena local run`."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    port: int = typer.Option(8080, "--port", min=1, max=65535, help="Port to bind."),
) -> None:
    """Serve a local run's generated artifact for browser preview."""
    dist_dir = run_dir.resolve() / "generation" / "dist"
    if not (dist_dir / "index.html").exists():
        raise VisArenaError(f"Missing preview artifact: {dist_dir / 'index.html'}")
    _serve_directory_forever(dist_dir, host, port)


@results_app.command("preview")
def preview_result(result_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Print an HTML preview URL for a task-level result."""
    client = _client(server_url, token)
    try:
        typer.echo(client.get_job_preview_url(result_id))
    finally:
        client.close()


@llm_app.command("token")
def llm_token(provider: str, model: str, purpose: str = "generation", server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Request a short-lived cloud LLM token. Intended for cloud evaluation sandboxes."""
    client = _client(server_url, token)
    try:
        llm = client.request_llm_token(provider=provider, model=model, purpose=purpose)
        typer.echo(llm.model_dump_json(indent=2))
    finally:
        client.close()


@admin_rounds_app.command("list")
def admin_rounds_list(limit: int = typer.Option(20, "--limit", min=1), server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """List peer-review rounds. Requires admin access."""
    client = _client(server_url, token)
    try:
        for round_item in client.list_rounds(limit=limit):
            typer.echo(f"{round_item['id']}\t{round_item['name']}\t{round_item['status']}\t{round_item.get('starts_at') or ''}\t{round_item.get('ends_at') or ''}")
    finally:
        client.close()


@admin_rounds_app.command("open")
def admin_rounds_open(
    name: str = typer.Option(..., "--name", "-n"),
    starts_at: Optional[str] = typer.Option(None, "--starts-at"),
    ends_at: Optional[str] = typer.Option(None, "--ends-at"),
    interval_seconds: Optional[int] = typer.Option(None, "--interval-seconds", min=1),
    server_url: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """Open a peer-review round. Requires admin access."""
    client = _client(server_url, token)
    try:
        round_item = client.open_round(name=name, starts_at=starts_at, ends_at=ends_at, interval_seconds=interval_seconds)
        typer.echo(f"{round_item['id']}\t{round_item['name']}\t{round_item['status']}")
    finally:
        client.close()


@admin_rounds_app.command("close")
def admin_rounds_close(round_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Snapshot participants and queue generation jobs for a round."""
    client = _client(server_url, token)
    try:
        detail = client.close_round(round_id)
        typer.echo(_format_round_detail(detail))
    finally:
        client.close()


@admin_rounds_app.command("start-peer-review")
def admin_rounds_start_peer_review(round_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Queue cross-user peer-review jobs for a round."""
    client = _client(server_url, token)
    try:
        detail = client.start_peer_review_round(round_id)
        typer.echo(_format_round_detail(detail))
    finally:
        client.close()


@admin_rounds_app.command("status")
def admin_rounds_status(round_id: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Show round participants, job counts, and evaluation counts."""
    client = _client(server_url, token)
    try:
        detail = client.get_round(round_id)
        typer.echo(_format_round_detail(detail))
    finally:
        client.close()


@admin_rounds_app.command("leaderboard")
def admin_rounds_leaderboard(round_id: str, limit: int = typer.Option(100, "--limit", min=1), server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Show the evaluations-based leaderboard for one round."""
    client = _client(server_url, token)
    try:
        for item in client.round_leaderboard(round_id, limit=limit):
            score = "" if item.get("round_score") is None else f"{float(item['round_score']):.2f}"
            typer.echo(f"{item['submission_id']}\t{item['submission_name']}\t{item['owner_name']}\t{score}")
    finally:
        client.close()


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _format_round_detail(detail: dict) -> str:
    jobs = detail.get("jobs") or []
    evaluations = detail.get("evaluations") or []
    parts = [
        f"{detail['id']}\t{detail['name']}\t{detail['status']}",
        f"participants={len(detail.get('participants') or [])}",
        f"jobs={_summarize_job_statuses(jobs)}",
        f"evaluations={_summarize_job_statuses(evaluations)}",
    ]
    leaderboard = detail.get("leaderboard") or []
    if leaderboard:
        top = leaderboard[0]
        score = "" if top.get("round_score") is None else f"{float(top['round_score']):.2f}"
        parts.append(f"top={top.get('submission_name')}:{score}")
    return "  ".join(parts)


def _local_env(agent_dir: Path, env_file: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    candidates = [env_file.resolve()] if env_file else [Path.cwd() / ".env", Path.cwd() / ".env.local", agent_dir / ".env", agent_dir / ".env.local"]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(line)
            if parsed is None:
                continue
            key, value = parsed
            env.setdefault(key, value)
    return env


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].strip()
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return key, value


def _resolve_local_task(
    dataset_id: str | None,
    task_path: Path | None,
    run_dir: Path,
    server_url: str | None,
    token: str | None,
) -> Path:
    if dataset_id and task_path:
        raise VisArenaError("Pass either --dataset or --task, not both.")
    if dataset_id:
        client = _client(server_url, token)
        try:
            dataset = client.resolve_dataset(dataset_id)
            bundle_path = client.download_dataset(dataset.id, run_dir / "dataset.zip")
        finally:
            client.close()
        return _prepare_local_task(bundle_path, run_dir / "input")
    if task_path:
        return _prepare_local_task(task_path.resolve(), run_dir / "input")
    raise VisArenaError("Pass --dataset monthly-sales, or use --task for a local task folder/ZIP.")


def _prepare_local_task(task_path: Path, extract_dir: Path) -> Path:
    if task_path.is_dir():
        return task_path
    if not task_path.exists():
        raise VisArenaError(f"Task path does not exist: {task_path}")
    if task_path.suffix.lower() != ".zip":
        raise VisArenaError(f"Task path must be a directory or .zip file: {task_path}")

    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(task_path) as archive:
        _safe_extract_zip(archive, extract_dir)
    task_files = sorted(path for path in extract_dir.rglob("task.md") if "__MACOSX" not in path.parts)
    if not task_files:
        raise VisArenaError(f"Dataset ZIP does not contain task.md: {task_path}")
    if len(task_files) > 1:
        options = "\n".join(str(path.parent.relative_to(extract_dir)) for path in task_files)
        raise VisArenaError(f"Dataset ZIP contains multiple tasks. Extract it and pass one task directory:\n{options}")
    return task_files[0].parent


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for info in archive.infolist():
        member_path = target_root / info.filename
        resolved = member_path.resolve()
        if target_root != resolved and target_root not in resolved.parents:
            raise VisArenaError(f"Unsafe ZIP path: {info.filename}")
        if info.is_dir():
            resolved.mkdir(parents=True, exist_ok=True)
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, resolved.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _run_agent_command(agent_dir: Path, args: list[str], log_path: Path, env: dict[str, str]) -> None:
    command = _agent_command(agent_dir, args)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        log.flush()
        proc = subprocess.run(command, cwd=agent_dir, env=env, stdout=log, stderr=subprocess.STDOUT, text=True, check=False)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        raise VisArenaError(f"Agent command failed after {elapsed:.1f}s: {' '.join(command)}. See {log_path}")
    typer.echo(f"  {args[0]} ok ({elapsed:.1f}s)")


def _agent_command(agent_dir: Path, args: list[str]) -> list[str]:
    if (agent_dir / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "./agent.py", *args]
    return [sys.executable, str(agent_dir / "agent.py"), *args]


@contextmanager
def _serve_directory(directory: Path):
    server = _make_http_server(directory, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/index.html"
    finally:
        server.shutdown()
        server.server_close()


def _serve_directory_forever(directory: Path, host: str, port: int) -> None:
    server = _make_http_server(directory, host, port)
    actual_host, actual_port = server.server_address
    typer.echo(f"Serving {directory}")
    typer.echo(f"http://{actual_host}:{actual_port}/index.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("Stopped.")
    finally:
        server.server_close()


def _make_http_server(directory: Path, host: str, port: int):
    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass

    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731

    class _ReusableTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    return _ReusableTCPServer((host, port), handler)


def main() -> None:
    try:
        app()
    except VisArenaError as exc:
        typer.echo(f"Vis Arena API error: {exc}", err=True)
        # SystemExit (not typer.Exit) so the interpreter exits cleanly without
        # printing a traceback. typer.Exit is a click exception, not a
        # SystemExit subclass, so it would otherwise escape and trigger Python's
        # default uncaught-exception handler.
        raise SystemExit(1) from exc
