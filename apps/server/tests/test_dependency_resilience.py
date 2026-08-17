"""PyPI-flakiness hardening: persistent package cache + retried dependency priming.

Transient 502s from files.pythonhosted.org killed 4 real generation runs
(Aug 15-17) at the install step, before the participant's agent ever ran.
"""
from __future__ import annotations

import subprocess

import pytest

from vis_arena_server import evaluator
from vis_arena_server.settings import settings


def _docker_cmd(monkeypatch, tmp_path, cache_dir: str) -> list[str]:
    (tmp_path / "reports").mkdir()
    monkeypatch.setattr(settings, "evaluator_uv_cache_dir", cache_dir)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Done:
            stdout = ""
            returncode = 0

        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(evaluator, "create_token", lambda owner_id: "tok")
    evaluator.run_docker(
        tmp_path,
        {"id": "job-1", "owner_id": "user-1", "job_type": "generation"},
        phase="generation",
    )
    return captured["cmd"]


def test_uv_cache_volume_mounted_when_configured(monkeypatch, tmp_path) -> None:
    cmd = _docker_cmd(monkeypatch, tmp_path, "/opt/vis-arena/uv-cache")
    assert "/opt/vis-arena/uv-cache:/arena/.uv-cache" in cmd
    # pip's bootstrap cache lives inside the same persistent mount
    assert "PIP_CACHE_DIR=/arena/.uv-cache/pip" in cmd


def test_no_cache_volume_by_default(monkeypatch, tmp_path) -> None:
    cmd = _docker_cmd(monkeypatch, tmp_path, "")
    assert not any(":/arena/.uv-cache" in part for part in cmd if isinstance(part, str) and part.count(":") == 1)


@pytest.mark.parametrize("phase", ["generation", "evaluation"])
def test_container_script_primes_dependencies_with_retry(phase: str) -> None:
    script = evaluator.render_container_script(phase)
    # a dedicated, retried priming step runs before any agent step
    assert "prime_deps" in script
    assert "retry_pypi" in script
    # bootstrap pip install is retried too
    assert script.count("retry_pypi python -m pip install") >= 1
    # priming is its own logged step so infra failures aren't blamed on the agent
    assert f"run_phase {phase} deps prime_deps" in script
    # priming must precede the first agent step in the script text
    assert script.index("deps prime_deps") < script.index(
        "info" if phase == "generation" else "agent.py evaluate"
    )
