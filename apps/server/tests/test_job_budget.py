"""Per-job-type token budgets: participants keep the budget they competed under."""
from __future__ import annotations

import pytest

from vis_arena_server import llm
from vis_arena_server.settings import settings


def test_generation_uses_the_participant_budget(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens_per_job", 1_000_000)
    monkeypatch.setattr(settings, "llm_max_tokens_per_central_eval", 2_000_000)
    assert llm.budget_for_job({"job_type": "generation"}) == 1_000_000
    assert llm.budget_for_job({"job_type": "peer_evaluation"}) == 1_000_000
    assert llm.budget_for_job({"job_type": None}) == 1_000_000


def test_central_evaluation_gets_its_own_headroom(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens_per_job", 1_000_000)
    monkeypatch.setattr(settings, "llm_max_tokens_per_central_eval", 2_000_000)
    assert llm.budget_for_job({"job_type": "central_evaluation"}) == 2_000_000


def test_central_budget_defaults_to_the_normal_one_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens_per_job", 1_000_000)
    monkeypatch.setattr(settings, "llm_max_tokens_per_central_eval", 0)
    assert llm.budget_for_job({"job_type": "central_evaluation"}) == 1_000_000
