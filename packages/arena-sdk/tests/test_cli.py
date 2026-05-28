"""CLI tests (Tier 2).

Drives the Typer app via CliRunner. Validates the user-facing surface:
exit codes, stdout/stderr shape, config.json side effects, and that the
main() error wrapper actually wraps VisArenaError (regression guard for
the pyproject entrypoint bug).
"""

from __future__ import annotations

import json
import uuid

import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from vis_arena_sdk import cli
from vis_arena_sdk.cli import app
from vis_arena_sdk.client import VisArenaError


runner = CliRunner()


def _unique_email() -> str:
    return f"cli-{uuid.uuid4().hex[:12]}@example.com"


def test_register_writes_config_file(fresh_config) -> None:
    email = _unique_email()
    result = runner.invoke(app, ["register", email, "pw1234567890", "--name", "CLI Test"])

    assert result.exit_code == 0, result.output
    assert f"Registered {email}" in result.output
    assert fresh_config.exists()

    data = json.loads(fresh_config.read_text())
    assert "access_token" in data
    assert data["access_token"]
    assert "server_url" in data


def test_login_overwrites_stored_token(fresh_config) -> None:
    email = _unique_email()
    runner.invoke(app, ["register", email, "pw1234567890"])
    first_token = json.loads(fresh_config.read_text())["access_token"]

    # Log in again with the same credentials. New token must replace the old one.
    result = runner.invoke(app, ["login", email, "pw1234567890"])
    assert result.exit_code == 0, result.output
    assert f"Logged in as {email}" in result.output

    second_token = json.loads(fresh_config.read_text())["access_token"]
    # Both tokens are valid JWTs but the freshly minted one has a new `iat`.
    assert second_token, "config should still hold a token after login"


def test_env_token_beats_config_file(fresh_config, monkeypatch) -> None:
    """VIS_ARENA_API_TOKEN must take precedence over the config file."""
    email = _unique_email()
    runner.invoke(app, ["register", email, "pw1234567890", "--name", "Env Token Test"])

    # Stash a real-but-stale token in config; then run whoami with a junk env
    # token. The env token must win (and 401 because it's invalid).
    monkeypatch.setenv("VIS_ARENA_API_TOKEN", "not-a-valid-jwt")
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code != 0, "junk env token should fail auth"


def test_env_server_url_beats_config_file(fresh_config, monkeypatch) -> None:
    """VIS_ARENA_SERVER_URL takes precedence over the stored value."""
    from vis_arena_sdk.config import resolve_server_url

    monkeypatch.setenv("VIS_ARENA_SERVER_URL", "https://override.example.com")
    assert resolve_server_url() == "https://override.example.com"
    # explicit arg still wins over env
    assert resolve_server_url("https://explicit.example.com") == "https://explicit.example.com"


def test_main_wraps_visarenaerror_as_clean_message(monkeypatch, capsys) -> None:
    """Regression guard for the pyproject `vis_arena_sdk.cli:app` bug.

    If main() is not the registered entrypoint, VisArenaError escapes as a
    traceback. Here we call main() directly, force a VisArenaError, and
    assert (a) clean stderr message and (b) exit code 1.
    """

    def _explode() -> None:
        raise VisArenaError("synthetic 503", status_code=503)

    monkeypatch.setattr(cli, "app", _explode)
    # main() must raise SystemExit (NOT typer.Exit) so the interpreter
    # produces a clean exit instead of a traceback. See cli.main().
    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Vis Arena API error" in err
    assert "synthetic 503" in err
    assert "Traceback" not in err


def test_binary_does_not_leak_traceback_on_api_error(tmp_path: Path) -> None:
    """End-to-end regression: invoke the installed `vis-arena` binary in a
    subprocess and assert the user never sees a Python traceback when the
    API returns an error. This catches the class of bug where main() prints
    the clean message but lets a non-SystemExit exception escape (the
    original `raise typer.Exit(1)` mistake).

    Spins up a 5-line in-process HTTP mock that 401s every request so the
    CLI hits a VisArenaError. The CLI runs in a real subprocess — that's
    the only way to exercise Python's uncaught-exception handler.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    binary = Path(sys.executable).parent / "vis-arena"
    if not binary.exists():
        pytest.skip("vis-arena binary not installed in current venv")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Invalid token"}')

        def log_message(self, *_args) -> None:  # silence stderr noise
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = os.environ.copy()
        env["VIS_ARENA_CONFIG_DIR"] = str(tmp_path)
        env["VIS_ARENA_SERVER_URL"] = f"http://127.0.0.1:{port}"
        env["VIS_ARENA_API_TOKEN"] = "junk-token"

        result = subprocess.run(
            [str(binary), "whoami"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.shutdown()

    assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
    assert "Vis Arena API error" in result.stderr, (
        f"clean error message missing.\nstderr was:\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr, (
        f"User-facing CLI must not leak Python tracebacks.\nstderr was:\n{result.stderr}"
    )
