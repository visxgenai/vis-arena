from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version

import typer

from .client import VisArenaClient, VisArenaError
from .config import resolve_server_url, resolve_token


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
