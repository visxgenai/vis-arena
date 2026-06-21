from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .evaluator import run_job
from .settings import settings


def run() -> None:
    job_id = _required_env("VIS_ARENA_JOB_ID")
    token = _required_env("VIS_ARENA_RUNNER_TOKEN")
    server_url = os.environ.get("VIS_ARENA_SERVER_URL") or settings.public_base_url
    client = RunnerClient(server_url.rstrip("/"), job_id, token)
    job = client.lease()
    stop_event = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(client, stop_event), daemon=True)
    heartbeat.start()
    try:
        result = run_job(job, use_docker=False, update_intermediate_metadata=False)
        client.complete(result)
    except Exception as exc:
        client.fail(str(exc))
        raise
    finally:
        stop_event.set()
        heartbeat.join(timeout=5)


class RunnerClient:
    def __init__(self, server_url: str, job_id: str, token: str) -> None:
        self.server_url = server_url
        self.job_id = job_id
        self.token = token

    def lease(self) -> dict[str, Any]:
        return self._request("GET", f"/internal/jobs/{self.job_id}/lease")["job"]

    def heartbeat(self) -> None:
        self._request("POST", f"/internal/jobs/{self.job_id}/heartbeat", {})

    def complete(self, result: dict[str, Any]) -> None:
        self._request("POST", f"/internal/jobs/{self.job_id}/complete", result)

    def fail(self, error: str) -> None:
        self._request("POST", f"/internal/jobs/{self.job_id}/fail", {"error": error})

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.server_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))


def _heartbeat_loop(client: RunnerClient, stop_event: threading.Event) -> None:
    interval = max(5, settings.runner_heartbeat_interval_seconds)
    while not stop_event.wait(interval):
        try:
            client.heartbeat()
        except Exception:
            # A transient heartbeat failure should not kill a running evaluation.
            pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value
