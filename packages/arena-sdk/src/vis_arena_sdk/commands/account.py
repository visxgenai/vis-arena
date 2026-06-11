from __future__ import annotations

from typing import Optional

import typer

from ..client import VisArenaClient, VisArenaError
from ..cli_runtime import _check_cli_version, _cli_version, _client
from ..config import load_config, resolve_server_url, save_config


def register_account_commands(app: typer.Typer, profile_app: typer.Typer) -> None:
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

    @profile_app.callback(invoke_without_command=True)
    def profile(
        ctx: typer.Context,
        server_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        """Show the authenticated user's profile."""
        if ctx.invoked_subcommand is not None:
            return
        client = _client(server_url, token)
        try:
            _check_cli_version(client, enforce_minimum=True)
            typer.echo(_format_user(client.me()))
        finally:
            client.close()

    @profile_app.command("show")
    def profile_show(server_url: Optional[str] = None, token: Optional[str] = None) -> None:
        """Show the authenticated user's profile."""
        client = _client(server_url, token)
        try:
            _check_cli_version(client, enforce_minimum=True)
            typer.echo(_format_user(client.me()))
        finally:
            client.close()

    @profile_app.command("set-name")
    def profile_set_name(name: str, server_url: Optional[str] = None, token: Optional[str] = None) -> None:
        """Update the display name shown on the leaderboard."""
        client = _client(server_url, token)
        try:
            _check_cli_version(client, enforce_minimum=True)
            user = client.update_me(name)
            typer.echo(f"Updated profile: {user['name']} <{user['email']}>")
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


def _format_user(user: dict) -> str:
    return f"{user.get('id')}\t{user.get('email')}\t{user.get('name') or ''}"
