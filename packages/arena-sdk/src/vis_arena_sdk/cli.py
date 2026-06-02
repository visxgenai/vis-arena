from __future__ import annotations

import json
import stat
import time
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
llm_app = typer.Typer(help="Cloud LLM token commands")
app.add_typer(datasets_app, name="datasets")
app.add_typer(submissions_app, name="submissions")
app.add_typer(results_app, name="results")
app.add_typer(llm_app, name="llm")


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
    dataset_id: Optional[str] = typer.Option(None, "--dataset-id", "--dataset", help="Dataset to run this submission against."),
    server_url: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """Upload an agent submission."""
    _upload_submission(path, name=name or path.stem, dataset_id=dataset_id, server_url=server_url, token=token)


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
        path = client.download_dataset(dataset_id, output)
        typer.echo(str(path))
    finally:
        client.close()


@submissions_app.command("upload")
def submissions_upload(path: Path, name: str, dataset_id: Optional[str] = None, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    """Upload an agent submission ZIP."""
    _upload_submission(path, name=name, dataset_id=dataset_id, server_url=server_url, token=token)


def _upload_submission(path: Path, name: str, dataset_id: Optional[str] = None, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
    client = _client(server_url, token)
    try:
        dataset = client.resolve_dataset(dataset_id) if dataset_id else None
        submission = client.upload_submission(
            path, name=name, dataset_id=dataset.id if dataset else None
        )
        target = f'"{dataset.name}"' if dataset else "all public datasets"
        typer.echo(f"Submission {submission.id} queued against {target}.")
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
        if submission.status in {"queued", "running", "uploading"} or any(job.get("status") in {"queued", "running"} for job in jobs):
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
            typer.echo(f"{result['id']}\t{result['task_id']}\t{result['status']}\t{duration}\t{preview}")
    finally:
        client.close()


def _format_submission_status(status: str, score: float | None, jobs: list[dict], usage: dict) -> str:
    job_statuses = {str(job.get("status") or "unknown") for job in jobs}
    run_seconds = max((float(job["run_seconds"]) for job in jobs if job.get("run_seconds") is not None), default=None)
    total_tokens = int((usage.get("summary") or {}).get("total_tokens") or 0)
    parts = [status]
    if job_statuses and (len(job_statuses) > 1 or status not in job_statuses):
        parts.append("tasks=" + ",".join(sorted(job_statuses)))
    if run_seconds is not None:
        parts.append(f"runtime={run_seconds:.1f}s")
    if total_tokens:
        parts.append(f"tokens={total_tokens:,}")
    if score is not None:
        parts.append(f"score={score:.2f}")
    return "  ".join(parts)


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
