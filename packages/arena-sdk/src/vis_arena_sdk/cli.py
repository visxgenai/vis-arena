from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .client import VisArenaClient, VisArenaError
from .config import load_config, resolve_server_url, resolve_token, save_config

app = typer.Typer(help="Vis Arena command line client")
datasets_app = typer.Typer(help="Dataset commands")
submissions_app = typer.Typer(help="Submission commands")
llm_app = typer.Typer(help="Cloud LLM token commands")
app.add_typer(datasets_app, name="datasets")
app.add_typer(submissions_app, name="submissions")
app.add_typer(llm_app, name="llm")


def _client(server_url: str | None = None, token: str | None = None) -> VisArenaClient:
    return VisArenaClient(base_url=resolve_server_url(server_url), token=resolve_token(token))


@app.command()
def register(email: str, password: str, name: Optional[str] = None, server_url: Optional[str] = None) -> None:
    """Create an arena account and store the API token."""
    client = _client(server_url, None)
    try:
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
        typer.echo(client.me())
    finally:
        client.close()


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
    client = _client(server_url, token)
    try:
        submission = client.upload_submission(path, name=name, dataset_id=dataset_id)
        typer.echo(f"{submission.id}\t{submission.name}\t{submission.status}")
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
