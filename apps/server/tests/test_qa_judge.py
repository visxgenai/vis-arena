"""QA-judge mechanism tests — run entirely on the public dummy fixture.

The real question set is private (never in this repo); these tests prove the
grading harness, the aggregate-only report, and the no-leak guarantees using
questions.example.json only.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vis_arena_server.db import connect, init_db, now_iso
from vis_arena_server.main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
JUDGE_DIR = REPO_ROOT / "submissions" / "qa-judge-template"

spec = importlib.util.spec_from_file_location("qa_judge_agent", JUDGE_DIR / "example_agent.py")
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)

QUESTIONS = qa.load_questions(JUDGE_DIR / "questions.example.json")


def test_fixture_loads_and_validates():
    assert [q["id"] for q in QUESTIONS] == ["q1", "q2", "q3"]


def test_normalization():
    assert qa.normalize_answer("  The Example   Dashboard! ") == "example dashboard"
    assert qa.normalize_answer(None) == ""


def test_text_answers_with_variants():
    q = QUESTIONS[0]  # answer: Example Dashboard
    assert qa.is_correct(q, "Example Dashboard")
    assert qa.is_correct(q, "the example dashboard")
    assert qa.is_correct(q, "It is the Example Dashboard")
    assert not qa.is_correct(q, "Dashboard")           # bare substring of a different span
    assert not qa.is_correct(q, "unknown")
    assert not qa.is_correct(q, "")


def test_number_answers_exact():
    q = QUESTIONS[1]  # answer: 3
    assert qa.is_correct(q, "3")
    assert qa.is_correct(q, "3 charts")
    assert not qa.is_correct(q, "13")
    assert not qa.is_correct(q, "3 charts and 2 tables")  # extra number = ambiguous


def test_grade_aggregate_and_no_leak():
    report = qa.grade(QUESTIONS, {"q1": "example dashboard", "q2": "3", "q3": "Beta"})
    assert report["score"] == pytest.approx(66.7, abs=0.1)
    assert [r["correct"] for r in report["per_question"]] == [True, True, False]
    dumped = json.dumps(report).lower()
    # the report must never contain question text or (non-trivial) expected answers —
    # bare single digits can coincide with counts/scores, so only identifying strings count
    for q in QUESTIONS:
        assert q["question"].lower() not in dumped
        answer = qa.normalize_answer(q["answer"])
        if len(answer) >= 4:
            assert answer not in dumped
    assert set(report["per_question"][0].keys()) == {"id", "correct"}


def test_missing_answers_score_zero():
    report = qa.grade(QUESTIONS, {})
    assert report["score"] == 0.0


def test_private_judge_dir_is_gitignored():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", ".judge/questions.json"],
        capture_output=True,
    )
    assert result.returncode == 0, ".judge/ must be gitignored — the real question set lives there"


def test_central_evaluation_report_endpoint_is_gated():
    init_db()
    job_id = f"central-{uuid.uuid4().hex[:12]}"
    now = now_iso()
    with connect() as db:
        db.execute(
            "insert into jobs (id, submission_id, job_type, status, evaluation_report_s3_key, created_at, updated_at) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (job_id, "whatever", "central_evaluation", "succeeded", "jobs/x/evaluation/report.json", now, now),
        )
    client = TestClient(app)
    response = client.get(f"/v1/jobs/{job_id}/evaluation-report")
    assert response.status_code == 404


# --- two-aspect (QA + rubric) grading ---

FULL_RUBRIC = [
    {"id": "data_fidelity", "score": 4, "evidence": "totals consistent"},
    {"id": "insightfulness", "score": 3, "evidence": "headline pattern"},
    {"id": "narrative_coherence", "score": 4, "evidence": "clear arc"},
    {"id": "visual_craft", "score": 5, "evidence": "clean encodings"},
    {"id": "functionality", "score": 4, "evidence": "filters work"},
]


def test_rubric_score_complete_and_clamped():
    assert qa.rubric_score(FULL_RUBRIC) == 80.0  # (4+3+4+5+4)*4
    clamped = [dict(r, score=9) for r in FULL_RUBRIC]
    assert qa.rubric_score(clamped) == 100.0


def test_rubric_score_partial_is_none():
    assert qa.rubric_score(FULL_RUBRIC[:4]) is None
    assert qa.rubric_score(None) is None
    broken = [dict(r) for r in FULL_RUBRIC]
    broken[0]["score"] = "n/a"
    assert qa.rubric_score(broken) is None


def test_grade_combined_default_direct_sum():
    answers = {"q1": "example dashboard", "q2": "3", "q3": "Alpha"}  # QA = 100
    report = qa.grade_combined(QUESTIONS, answers, FULL_RUBRIC)
    assert report["score"] == pytest.approx(180.0)  # 100 + 80, direct sum
    assert report["max_score"] == 200
    assert report["metadata"]["qa_score"] == 100.0
    assert report["metadata"]["rubric_score"] == 80.0
    assert report["metadata"]["combine"] == "sum"
    assert len(report["criteria"]) == 5
    dumped = json.dumps(report).lower()
    for q in QUESTIONS:
        assert q["question"].lower() not in dumped


def test_grade_combined_weighted_mode_still_available():
    answers = {"q1": "example dashboard", "q2": "3", "q3": "Alpha"}
    report = qa.grade_combined(QUESTIONS, answers, FULL_RUBRIC, combine="weighted", qa_weight=0.5)
    assert report["score"] == pytest.approx(90.0)  # 0.5*100 + 0.5*80
    assert report["max_score"] == 100
    assert qa.grade_combined(QUESTIONS, answers, FULL_RUBRIC, combine="weighted", qa_weight=1.0)["score"] == pytest.approx(100.0)


def test_grade_combined_rubric_missing_falls_back_to_qa():
    report = qa.grade_combined(QUESTIONS, {"q2": "3"}, None)
    assert report["score"] == pytest.approx(33.3, abs=0.1)
    assert report["max_score"] == 100
    assert report["metadata"]["rubric_score"] is None
    assert report["criteria"] == []
